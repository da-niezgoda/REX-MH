
import streamlit as st
import pandas as pd
from datetime import datetime
import os
import json
import time
from st_mui_table import st_mui_table
from pathlib import Path


# Configure page
st.set_page_config(
    page_title="REX Zones Humides",
    page_icon="🌿",
    layout="wide",
    initial_sidebar_state="collapsed"
)


def load_css(file):
    with open(file) as f:
        st.html(f"<style>{f.read()}</style>")

css_path = "styles.css"
load_css(css_path)


def load_schema(schema_name):
    """Load the REX JSON schema from the schemas directory"""

    schema_path = Path(schema_name)
    
    if not schema_path.exists():
        st.error(f"Schema file not found: {schema_path}")
        return None
    
    try:
        with open(schema_path, 'r', encoding='utf-8') as f:
            schema = json.load(f)
        return schema
    except Exception as e:
        st.error(f"Error loading schema REX.schema.json: {str(e)}")
        return None


def load_prompt(prompt_name, schema=None):
    """
    Load a markdown prompt file and optionally replace schema placeholder

    Args:
        prompt_name: Name of the prompt file (e.g., 'REXPrompt.md')
        schema: Optional JSON schema dict to inject into the prompt

    Returns:
        str: The prompt content with schema injected if provided
    """
    prompt_path = Path(prompt_name)

    if not prompt_path.exists():
        st.error(f"Prompt file not found: {prompt_path}")
        return None

    try:
        with open(prompt_path, 'r', encoding='utf-8') as f:
            prompt_content = f.read()

        # Replace schema placeholder if schema is provided
        if schema is not None:
            schema_json = json.dumps(schema, indent=2, ensure_ascii=False)
            prompt_content = prompt_content.replace("{{ SCHEMA_JSON }}", schema_json)

        return prompt_content
    except Exception as e:
        st.error(f"Error loading prompt {prompt_name}: {str(e)}")
        return None


def clean_document(ocr_response):
    """
    Transform pages format from index/markdown to page_number/content.

    Args:
        ocr_response: OCR response object with pages

    Returns:
        JSON string with transformed format
    """
    transformed = {
        "pages": [
            {
                "page_number": page.index + 1,
                "content": page.markdown
            }
            for page in ocr_response.pages
        ]
    }

    return json.dumps(transformed, indent=2)


def clean_pages(ocr_response, start_page, end_page):
    """
    Extract specific pages from OCR response and format them.
    
    Args:
        ocr_response: OCR response object with pages
        start_page: Starting page number (1-indexed)
        end_page: Ending page number (1-indexed, inclusive)
    
    Returns:
        JSON string with transformed format for the specified page range
    """
    all_pages = ocr_response.pages
    
    # Filter pages by range (converting from 1-indexed to 0-indexed)
    filtered_pages = [
        page for page in all_pages 
        if start_page <= (page.index + 1) <= end_page
    ]
    
    transformed = {
        "pages": [
            {
                "page_number": page.index + 1,
                "content": page.markdown
            }
            for page in filtered_pages
        ]
    }
    
    return json.dumps(transformed, indent=2)


def parse_pdf_document(file_content, filename, progress_callback=None):
    """
    Parse PDF document using Mistral API OCR
    
    Args:
        file_content: Binary content of the PDF file
        filename: Name of the file
        progress_callback: Optional callback function to update progress
                          Should accept (progress: float, status: str)
    
    Returns:
        dict: Parsed data structure matching the schema
    """
    try:
        # Initialize Mistral client
        api_key = st.secrets['MISTRAL_API_KEY']
        if not api_key:
            raise ValueError("MISTRAL_API_KEY not found in environment variables")
        
        from mistralai import Mistral
        client = Mistral(api_key=api_key)
        
        # Define model to use
        model = "mistral-medium-latest"
        
        if progress_callback:
            progress_callback(0.1, "Upload du fichier vers Mistral...")
        
        # Upload the PDF file for OCR
        uploaded_pdf = client.files.upload(
            file={
                "file_name": filename,
                "content": file_content,
            },
            purpose="ocr"
        )

        # Get the signed URL
        signed_url = client.files.get_signed_url(file_id=uploaded_pdf.id)
        
        # Process the OCR result
        if progress_callback:
            progress_callback(0.2, "Traitement OCR du document...")
        
        ocr_response = client.ocr.process(
            model="mistral-ocr-latest",
            document={
                "type": "document_url",
                "document_url": signed_url.url,
            },
            include_image_base64=False
        )

        clean_doc = clean_document(ocr_response)

        # Extraction de la liste de projets
        if progress_callback:
            progress_callback(0.3, "Extraction de la liste de projets...")
        
        project_list_response = client.chat.complete(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": st.session_state.listPrompt
                },
                {
                    "role": "user",
                    "content": clean_doc,
                }
            ],
            response_format={
                "type": "json_object",
            }
        )

        project_list_json = project_list_response.choices[0].message.content
        print("Project list response:", project_list_json)
        
        if progress_callback:
            progress_callback(0.4, "Vérification des projets...")
        
        # Parse project list JSON to dict
        try:
            project_list_dict = json.loads(project_list_json)
        except json.JSONDecodeError as e:
            raise Exception(f"Erreur lors du parsing de la liste de projets: {str(e)}")
        
        # Get the list of projects
        projects = project_list_dict.get("Liste", [])
        
        if not projects:
            raise Exception("Aucun projet trouvé dans le document")
        
        if progress_callback:
            progress_callback(0.5, f"Extraction des données pour {len(projects)} projet(s)...")
        
        # Initialize parsed data list
        parsed_data = []
        
        # Calculate progress increment per project
        progress_per_project = 0.4 / len(projects) if projects else 0
        
        # Loop through project list
        for idx, project in enumerate(projects):
            try:
                start_page = project.get("PageDebut")
                end_page = project.get("PageFin")
                project_title = project.get("Titre", f"Projet {idx + 1}")
                
                if not start_page or not end_page:
                    print(f"Warning: Projet '{project_title}' n'a pas de pages définies, ignoré")
                    continue
                
                if progress_callback:
                    current_progress = 0.5 + (idx * progress_per_project)
                    progress_callback(
                        current_progress, 
                        f"Analyse du projet {idx + 1}/{len(projects)}: {project_title[:50]}..."
                    )
                
                # Extract pages for this specific project
                project_pages = clean_pages(ocr_response, start_page, end_page)
                
                # Analyze specific project with Mistral
                project_analysis_response = client.chat.complete(
                    model=model,
                    messages=[
                        {
                            "role": "system",
                            "content": st.session_state.REXPrompt
                        },
                        {
                            "role": "user",
                            "content": project_pages,
                        }
                    ],
                    response_format={
                        "type": "json_object",
                    }
                )
                
                project_data_json = project_analysis_response.choices[0].message.content
                
                # Parse project data
                try:
                    project_data = json.loads(project_data_json)
                    # Add page range information to the project data
                    project_data['_project_title'] = project_title
                    project_data['_page_debut'] = start_page
                    project_data['_page_fin'] = end_page
                    parsed_data.append(project_data)
                    print(f"Projet '{project_title}' analysé avec succès")
                except json.JSONDecodeError as e:
                    print(f"Warning: Erreur lors du parsing du projet '{project_title}': {str(e)}")
                    continue
                    
            except Exception as e:
                print(f"Warning: Erreur lors du traitement du projet {idx + 1}: {str(e)}")
                continue
        
        if not parsed_data:
            raise Exception("Aucun projet n'a pu être analysé avec succès")
        
        if progress_callback:
            progress_callback(1.0, f"Traitement terminé - {len(parsed_data)} projet(s) extrait(s)")
        
        # Return the list of parsed projects
        return parsed_data
        
    except Exception as e:
        if progress_callback:
            progress_callback(0, f"Erreur: {str(e)}")
        raise Exception(f"Erreur lors du traitement OCR: {str(e)}")


def process_uploaded_file(file, filename):
    """Process uploaded PDF file with progress tracking"""
    
    # Create progress container
    progress_container = st.container()
    
    with progress_container:
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        def update_progress(progress, status):
            progress_bar.progress(progress)
            status_text.text(status)
        
        try:
            # Parse the document
            parsed_data = parse_pdf_document(
                file, 
                filename,
                progress_callback=update_progress
            )
            
            # Store in session state
            st.session_state.last_parsed_data = {
                'filename': filename,
                'date': datetime.now().strftime("%d/%m/%Y %H:%M"),
                'projects': parsed_data
            }
            
            # Clear progress bar
            progress_bar.empty()
            status_text.empty()
            
            st.markdown(f'<div class="success-message">✅ Document traité avec succès - {len(parsed_data)} projet(s) extrait(s)</div>', 
                      unsafe_allow_html=True)
            time.sleep(2)
            st.rerun()
                
        except Exception as e:
            progress_bar.empty()
            status_text.empty()
            st.markdown(f'<div class="error-message">❌ Erreur lors du traitement: {str(e)}</div>', 
                      unsafe_allow_html=True)


def display_dashboard():
    """Display dashboard header"""
    st.markdown("""
    <div class="main-header">
        <h1>🌿 REX Zones Humides</h1>
        <h4>Extraction de retours d'expérience depuis PDF</h4>
    </div>
    """, unsafe_allow_html=True)



def format_expanded_data(doc_data):
    """Format document data for expanded view based on new schema structure"""
    if not doc_data:
        return "Aucune donnée disponible"

    html_content = '<div class="expanded-content">'

    # Presentation info (capitalized key)
    if 'Presentation' in doc_data:
        pres_data = doc_data['Presentation']
        if isinstance(pres_data, dict) and any(pres_data.values()):
            html_content += '<div class="field-group">'
            html_content += '<h4>📋 Informations du projet</h4>'
            field_labels = {
                'Titre': 'Titre',
                'Bassin': 'Bassin',
                "Nom de l'organisme": 'Nom de l\'organisme',
                'Localisation': 'Localisation',
                'Adresse précise': 'Adresse précise',
                'Région': 'Région'
            }
            for key, label in field_labels.items():
                value = pres_data.get(key, '')
                if value:
                    html_content += f'<div class="field-item"><span class="field-label">{label}:</span><span class="field-value">{value}</span></div>'
            html_content += '</div>'

    # Objectif (capitalized key)
    if 'Objectif' in doc_data:
        obj_data = doc_data['Objectif']
        if isinstance(obj_data, dict) and obj_data.get('objectifs'):
            html_content += '<div class="field-group">'
            html_content += '<h4>🎯 Objectif du maître d\'ouvrage</h4>'
            html_content += f'<div class="field-item"><span class="field-value">{obj_data["objectifs"]}</span></div>'
            html_content += '</div>'

    # Description (capitalized key)
    if 'Description' in doc_data:
        desc_data = doc_data['Description']
        if isinstance(desc_data, dict) and any(desc_data.values()):
            html_content += '<div class="field-group">'
            html_content += '<h4>📝 Description</h4>'
            if desc_data.get('resume'):
                html_content += f'<div class="field-item"><span class="field-label">Résumé:</span><span class="field-value">{desc_data["resume"][:500]}{"..." if len(desc_data["resume"]) > 500 else ""}</span></div>'
            if desc_data.get('publication_recueil'):
                html_content += f'<div class="field-item"><span class="field-label">Publication:</span><span class="field-value">{desc_data["publication_recueil"]}</span></div>'
            html_content += '</div>'

    # Enjeux (capitalized key)
    if 'Enjeux' in doc_data:
        enjeux_data = doc_data['Enjeux']
        if isinstance(enjeux_data, dict) and any(enjeux_data.values()):
            html_content += '<div class="field-group">'
            html_content += '<h4>🌱 Enjeux eau, biodiversité et climat</h4>'
            if enjeux_data.get('date_debut'):
                html_content += f'<div class="field-item"><span class="field-label">Date début:</span><span class="field-value">{enjeux_data["date_debut"]}</span></div>'
            if enjeux_data.get('date_fin'):
                html_content += f'<div class="field-item"><span class="field-label">Date fin:</span><span class="field-value">{enjeux_data["date_fin"]}</span></div>'
            if enjeux_data.get('enjeux') and isinstance(enjeux_data['enjeux'], list):
                html_content += f'<div class="field-item"><span class="field-label">Enjeux:</span><span class="field-value">{", ".join(enjeux_data["enjeux"])}</span></div>'
            html_content += '</div>'

    # Typologie (capitalized key)
    if 'Typologie' in doc_data:
        typo_data = doc_data['Typologie']
        if isinstance(typo_data, dict) and any(typo_data.values()):
            html_content += '<div class="field-group">'
            html_content += '<h4>🔧 Typologie - Ingénierie écologique</h4>'
            # Changed to handle flat dict structure
            for key, value in typo_data.items():
                if value and value != "":
                    formatted_key = key.replace('_', ' ').title()
                    html_content += f'<div class="field-item"><span class="field-label">{formatted_key}:</span><span class="field-value">{value}</span></div>'
            html_content += '</div>'

    # Directives européennes (capitalized key)
    if 'Directives' in doc_data:
        dir_data = doc_data['Directives']
        if isinstance(dir_data, dict) and any(dir_data.values()):
            html_content += '<div class="field-group">'
            html_content += '<h4>🇪🇺 Référence directives européennes</h4>'
            for key, value in dir_data.items():
                if value and value != "":
                    formatted_key = key.replace('_', ' ').title()
                    html_content += f'<div class="field-item"><span class="field-label">{formatted_key}:</span><span class="field-value">{value}</span></div>'
            html_content += '</div>'

    # Contexte réglementaire (capitalized key)
    if 'Contexte' in doc_data:
        ctx_data = doc_data['Contexte']
        if isinstance(ctx_data, dict) and any(ctx_data.values()):
            html_content += '<div class="field-group">'
            html_content += '<h4>⚖️ Contexte réglementaire</h4>'
            if ctx_data.get('contexte'):
                html_content += f'<div class="field-item"><span class="field-label">Contexte:</span><span class="field-value">{ctx_data["contexte"]}</span></div>'
            if ctx_data.get('autres'):
                html_content += f'<div class="field-item"><span class="field-label">Autres:</span><span class="field-value">{ctx_data["autres"]}</span></div>'
            html_content += '</div>'

    # Valorisation (capitalized key)
    if 'Valorisation' in doc_data:
        val_data = doc_data['Valorisation']
        if isinstance(val_data, dict) and any(val_data.values()):
            html_content += '<div class="field-group">'
            html_content += '<h4>🏆 Valorisation de l\'opération</h4>'
            for key, value in val_data.items():
                if value and value != "":
                    formatted_key = key.replace('_', ' ').title()
                    if key == 'url':
                        html_content += f'<div class="field-item"><span class="field-label">{formatted_key}:</span><span class="field-value"><a href="{value}" target="_blank">{value}</a></span></div>'
                    else:
                        html_content += f'<div class="field-item"><span class="field-label">{formatted_key}:</span><span class="field-value">{value}</span></div>'
            html_content += '</div>'

    # Travaux (capitalized key)
    if 'Travaux' in doc_data:
        travaux_data = doc_data['Travaux']
        if isinstance(travaux_data, dict) and travaux_data.get('surface_travaux'):
            html_content += '<div class="field-group">'
            html_content += '<h4>🗺️ Période et envergure des travaux</h4>'
            html_content += f'<div class="field-item"><span class="field-label">Surface des travaux:</span><span class="field-value">{travaux_data["surface_travaux"]}</span></div>'
            html_content += '</div>'

    # Documents (capitalized key)
    if 'Documents' in doc_data:
        doc_info = doc_data['Documents']
        if isinstance(doc_info, dict) and any(doc_info.values()):
            html_content += '<div class="field-group">'
            html_content += '<h4>📚 Documents</h4>'
            if doc_info.get('pages_extraire'):
                html_content += f'<div class="field-item"><span class="field-label">Pages à extraire:</span><span class="field-value">{doc_info["pages_extraire"]}</span></div>'
            if doc_info.get('recueil_complet'):
                html_content += f'<div class="field-item"><span class="field-label">Recueil complet:</span><span class="field-value"><a href="{doc_info["recueil_complet"]}" target="_blank">{doc_info["recueil_complet"]}</a></span></div>'
            html_content += '</div>'

    html_content += '</div>'
    return html_content



def display_file_upload():
    """Display file upload section"""
    st.markdown("### 📤 Importer un nouveau document PDF")

    uploaded_file = st.file_uploader(
        "Sélectionnez un fichier PDF",
        type=['pdf'],
        help="Glissez-déposez votre fichier PDF ici ou cliquez pour parcourir"
    )

    if uploaded_file is not None:
        col1, col2 = st.columns([1, 4])
        with col1:
            if st.button("📤 Envoyer", key="upload_btn"):
                process_uploaded_file(
                    uploaded_file.getvalue(), 
                    uploaded_file.name
                )

        with col2:
            st.info(f"Fichier sélectionné: {uploaded_file.name} ({uploaded_file.size} bytes)")


def flatten_project_data(project):
    """Flatten nested project data structure for Excel export"""
    flat_data = {}

    # Flatten Presentation
    if 'Presentation' in project and isinstance(project['Presentation'], dict):
        for key, value in project['Presentation'].items():
            flat_data[key] = value

    # Flatten Objectif
    if 'Objectif' in project and isinstance(project['Objectif'], dict):
        for key, value in project['Objectif'].items():
            flat_data[key] = value

    # Flatten Description
    if 'Description' in project and isinstance(project['Description'], dict):
        for key, value in project['Description'].items():
            flat_data[key] = value

    # Flatten Enjeux
    if 'Enjeux' in project and isinstance(project['Enjeux'], dict):
        for key, value in project['Enjeux'].items():
            # Convert lists to comma-separated strings
            if isinstance(value, list):
                flat_data[key] = ", ".join(str(v) for v in value)
            else:
                flat_data[key] = value

    # Flatten Typologie
    if 'Typologie' in project and isinstance(project['Typologie'], dict):
        for key, value in project['Typologie'].items():
            flat_data[key] = value

    # Flatten Directives
    if 'Directives' in project and isinstance(project['Directives'], dict):
        for key, value in project['Directives'].items():
            flat_data[key] = value

    # Flatten Contexte
    if 'Contexte' in project and isinstance(project['Contexte'], dict):
        for key, value in project['Contexte'].items():
            flat_data[key] = value

    # Flatten Valorisation
    if 'Valorisation' in project and isinstance(project['Valorisation'], dict):
        for key, value in project['Valorisation'].items():
            flat_data[key] = value

    # Flatten Travaux
    if 'Travaux' in project and isinstance(project['Travaux'], dict):
        for key, value in project['Travaux'].items():
            flat_data[key] = value

    # Flatten Documents
    if 'Documents' in project and isinstance(project['Documents'], dict):
        for key, value in project['Documents'].items():
            flat_data[key] = value

    # Add metadata fields
    if '_project_title' in project:
        flat_data['_project_title'] = project['_project_title']
    if '_page_debut' in project:
        flat_data['_page_debut'] = project['_page_debut']
    if '_page_fin' in project:
        flat_data['_page_fin'] = project['_page_fin']

    return flat_data


def create_excel_download(data):
    """Create Excel file from parsed data"""
    projects = data.get('projects', [])

    # Flatten all projects
    flattened_projects = [flatten_project_data(project) for project in projects]

    # Create DataFrame
    df = pd.DataFrame(flattened_projects)

    # Convert to Excel
    from io import BytesIO
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='REX')

    return output.getvalue()


def display_results_table():
    """Display table with parsed results from last upload"""
    if 'last_parsed_data' not in st.session_state:
        return

    data = st.session_state.last_parsed_data
    projects = data.get('projects', [])

    if not projects:
        return

    st.markdown("---")
    st.markdown(f"### 📊 Résultats de l'analyse - {data['filename']}")
    st.markdown(f"**{len(projects)} projet(s) extrait(s)** - {data['date']}")

    # Add download button
    excel_data = create_excel_download(data)
    filename_base = data['filename'].replace('.pdf', '')
    st.download_button(
        label="📥 Télécharger en Excel",
        data=excel_data,
        file_name=f"{filename_base}_REX_export.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        help="Télécharger les données extraites au format Excel"
    )

    # Prepare DataFrame for st_mui_table
    df_data = []
    for project in projects:
        titre = project.get("_project_title", "Sans titre")
        page_debut = project.get("_page_debut", "N/A")
        page_fin = project.get("_page_fin", "N/A")
        
        df_data.append({
            "Titre du projet": titre,
            "Page début": page_debut,
            "Page fin": page_fin,
            "Détails": format_expanded_data(project)
        })
    
        print(project)

    df = pd.DataFrame(df_data)
    
    # Display table with expandable details
    try:
        st_mui_table(
            df,
            customCss="""
/* Material 3 Expressive - Water & Biodiversity Theme */
:root {
    /* Primary - Deep Ocean */
    --md-sys-color-primary: #006A6B;
    --md-sys-color-on-primary: #FFFFFF;
    --md-sys-color-primary-container: #9CF0F2;
    --md-sys-color-on-primary-container: #002020;
    
    /* Secondary - Wetland Green */
    --md-sys-color-secondary: #4A6363;
    --md-sys-color-on-secondary: #FFFFFF;
    --md-sys-color-secondary-container: #CCE8E8;
    --md-sys-color-on-secondary-container: #051F1F;
    
    /* Tertiary - Fresh Water */
    --md-sys-color-tertiary: #00838F;
    --md-sys-color-on-tertiary: #FFFFFF;
    
    /* Surface & Background */
    --md-sys-color-surface: #FAFDFC;
    --md-sys-color-on-surface: #191C1C;
    --md-sys-color-surface-variant: #DAE5E4;
    --md-sys-color-surface-container-low: #F0F4F4;
    --md-sys-color-surface-container: #E6EBEB;
    --md-sys-color-surface-container-high: #DFE4E4;
    
    /* Outline & Borders */
    --md-sys-color-outline: #6F7979;
    --md-sys-color-outline-variant: #BEC8C8;
    
    /* Biodiversity Accent Colors */
    --bio-green: #2E7D32;
    --bio-blue: #0277BD;
    --bio-teal: #00695C;
    
    /* Elevation & Shadow */
    --elevation-1: 0 1px 3px rgba(0, 0, 0, 0.08), 0 1px 2px rgba(0, 106, 107, 0.06);
    --elevation-2: 0 3px 6px rgba(0, 0, 0, 0.1), 0 2px 4px rgba(0, 106, 107, 0.08);
    --elevation-3: 0 6px 12px rgba(0, 0, 0, 0.12), 0 4px 8px rgba(0, 106, 107, 0.1);
    --elevation-4: 0 12px 24px rgba(0, 0, 0, 0.14), 0 8px 16px rgba(0, 106, 107, 0.12);
    
    /* Expressive Radii */
    --radius-small: 12px;
    --radius-medium: 20px;
    --radius-large: 28px;
    --radius-extra-large: 36px;
    
    /* Transitions */
    --transition-standard: cubic-bezier(0.4, 0.0, 0.2, 1);
    --transition-decelerate: cubic-bezier(0.0, 0.0, 0.2, 1);
    --transition-accelerate: cubic-bezier(0.4, 0.0, 1, 1);
}

/* Table styling - M3 Expressive */
.MuiTableContainer-root {
    border-radius: var(--radius-large) !important;
    box-shadow: var(--elevation-2) !important;
    background: var(--md-sys-color-surface) !important;
    overflow: hidden !important;
}

.MuiTableHead-root {
    background: linear-gradient(135deg, var(--md-sys-color-primary-container) 0%, var(--md-sys-color-secondary-container) 100%) !important;
}

.MuiTableHead-root th {
    color: var(--md-sys-color-on-primary-container) !important;
    font-weight: 600 !important;
    font-size: 0.9375rem !important;
    letter-spacing: 0.5px !important;
    padding: 1.25rem 1rem !important;
}

.MuiTableBody-root tr {
    transition: all 0.2s var(--transition-standard) !important;
}

.MuiTableBody-root tr:hover {
    background: var(--md-sys-color-surface-container-low) !important;
    transform: translateX(2px) !important;
}

.MuiTableCell-root {
    border-bottom: 1px solid var(--md-sys-color-outline-variant) !important;
    padding: 1rem !important;
}

.MuiTablePagination-root {
    border-top: 2px solid var(--md-sys-color-primary-container) !important;
}

.MuiTablePagination-toolbar > p {
    margin: 0 !important;
    font-weight: 500 !important;
    color: var(--md-sys-color-on-surface) !important;
}

.MuiIconButton-root {
    color: var(--md-sys-color-primary) !important;
    transition: all 0.2s var(--transition-standard) !important;
}

.MuiIconButton-root:hover {
    background: var(--md-sys-color-primary-container) !important;
    transform: scale(1.1) !important;
}


                td.MuiTableCell-sizeSmall:first-child {
                    display: none;
                }

                .expanded-content .field-group {
                    padding: 1rem 0;
                }


            """,
            detailColumns=["Détails"],
            detailColNum=1,
            detailsHeader="",
            paginationSizes=[10, 25, 50],
            paginationLabel="Projets par page",
            enablePagination=True,
            showIndex=False,
            size="medium",
            stickyHeader=True,
            enable_sorting=False
        )
    except Exception as e:
        st.error(f"Erreur d'affichage du tableau: {str(e)}")
        # Fallback to expandable native Streamlit components
        for idx, project in enumerate(projects):
            presentation = project.get("presentation", {})
            titre = presentation.get("titre", f"Projet {idx + 1}")
            page_debut = project.get("_page_debut", "N/A")
            page_fin = project.get("_page_fin", "N/A")
            
            with st.expander(f"📄 {titre} (Pages {page_debut}-{page_fin})"):
                st.markdown(format_expanded_data(project), unsafe_allow_html=True)


def main():
    """Main application function"""
    
    # Load schema 
    if 'REXSchema' not in st.session_state:
        st.session_state.REXSchema = load_schema('REX.schema.json')
        if not st.session_state.REXSchema:
            st.error("Failed to load REX schema.")
            st.stop()

    if 'REXListSchema' not in st.session_state:
        st.session_state.REXListSchema = load_schema('REXlist.schema.json')
        if not st.session_state.REXListSchema:
            st.error("Failed to load REX list schema.")
            st.stop()

    # Load prompts with schema injection
    if 'REXPrompt' not in st.session_state:
        st.session_state.REXPrompt = load_prompt('REXPrompt.md', st.session_state.REXSchema)
        if not st.session_state.REXPrompt:
            st.error("Failed to load REX prompt.")
            st.stop()

    if 'listPrompt' not in st.session_state:
        st.session_state.listPrompt = load_prompt('listPrompt.md', st.session_state.REXListSchema)
        if not st.session_state.listPrompt:
            st.error("Failed to load list prompt.")
            st.stop()

    # Display dashboard
    display_dashboard()

    # File upload section
    display_file_upload()

    # Display results table if available
    display_results_table()


if __name__ == "__main__":
    main()


