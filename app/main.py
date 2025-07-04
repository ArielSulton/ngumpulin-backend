import os
import re
import json
import numpy as np
import tempfile
import shutil
from fastapi import FastAPI, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader
from langchain_pinecone import PineconeEmbeddings
from gliner import GLiNER
from dotenv import load_dotenv
from sklearn.metrics.pairwise import cosine_similarity
from datetime import datetime
from prometheus_fastapi_instrumentator import Instrumentator
import pickle
from sklearn.preprocessing import StandardScaler
import pandas as pd
import warnings
import logging
import mlflow
import mlflow.sklearn
from app.routers.agent import generate_feedback, AssignmentRequest
from app.utils.metrics import setup_metrics
from app.core.exceptions import ModelLoadError
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import List, Dict, Any
from mlflow.tracking import MlflowClient
import asyncio

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

# Constants
MODEL_NAME = "document-clustering"
MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")

# Initialize MLflow
mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
mlflow.set_experiment("document-processing")

class PredictionInput(BaseModel):
    sentences: int
    page: int
    timing: int
    plagiarism: float

class PredictionOutput(BaseModel):
    cluster: int
    confidence: float
    metadata: Dict[str, Any]

def load_best_model():
    try:
        model_path = "/app/models"
        if not os.path.exists(model_path):
            logger.warning("Model directory not found at /app/models")
            raise ModelLoadError("Model directory not found")
            
        model = mlflow.sklearn.load_model(model_path)
        logger.info(f"Successfully loaded model from {model_path}")
        return model
        
    except Exception as e:
        logger.error(f"Error loading model from shared volume: {str(e)}")
        raise ModelLoadError(f"Failed to load model from shared volume: {str(e)}")

def load_local_model():
    try:
        model_path = "/app/models"
        if os.path.exists(model_path) and os.path.isdir(model_path):
            logger.info(f"Loading model from MLflow structure at {model_path}")
            model = mlflow.sklearn.load_model(model_path)
            return model
            
        local_model_path = "/app/models/local_model.pkl"
        if os.path.exists(local_model_path) and os.path.isfile(local_model_path):
            logger.info(f"Loading model from pickle file {local_model_path}")
            with open(local_model_path, 'rb') as f:
                model = pickle.load(f)
            return model
            
        logger.warning("No model found in expected locations (/app/models or /app/models/local_model.pkl)")
        raise ModelLoadError("No model available locally")
    except Exception as e:
        logger.error(f"Error loading local model: {str(e)}")
        raise ModelLoadError(f"Failed to load local model: {str(e)}")

def load_scaler():
    try:
        scaler_path = "/mlflow/artifacts/scaler.pkl"
        if not os.path.exists(scaler_path):
            logger.error(f"Scaler file not found at {scaler_path}")
            raise FileNotFoundError(f"Scaler file not found at {scaler_path}")
            
        with open(scaler_path, 'rb') as f:
            scaler = pickle.load(f)
            logger.info(f"Successfully loaded scaler from {scaler_path}")
            return scaler
    except Exception as e:
        logger.error(f"Error loading scaler: {str(e)}")
        raise

def check_for_new_model():
    try:
        client = MlflowClient()
        experiment = client.get_experiment_by_name("document-processing")
        if not experiment:
            return None
            
        # Get all runs with model evaluation
        runs = client.search_runs(
            experiment_ids=[experiment.experiment_id],
            filter_string="tags.mlflow.runName = 'model_evaluation'",
            max_results=10
        )
        
        if not runs:
            return None
            
        # Get the latest run
        latest_run = runs[0]
        latest_model_path = latest_run.info.artifact_uri + "/model"
        
        # Check if this is different from current model
        current_model_path = "/app/models"
        if not os.path.exists(current_model_path):
            return latest_model_path
            
        # Compare model versions or timestamps
        current_model_time = os.path.getmtime(current_model_path)
        latest_model_time = latest_run.info.end_time / 1000  # Convert to seconds
        
        if latest_model_time > current_model_time:
            return latest_model_path
            
        return None
    except Exception as e:
        logger.error(f"Error checking for new model: {str(e)}")
        return None

def reload_model_and_scaler(app: FastAPI):
    try:
        new_model_path = check_for_new_model()
        if new_model_path:
            logger.info(f"New model found at {new_model_path}")
            
            # Load new model
            model = mlflow.sklearn.load_model(new_model_path)
            app.state.model = model
            logger.info("Model reloaded successfully")
            
            scaler = load_scaler()
            app.state.scaler = scaler
            logger.info("Scaler reloaded successfully")
            
            return True
    except Exception as e:
        logger.error(f"Error reloading model: {str(e)}")
    return False

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up the application...")
    try:
        model = load_best_model()
        logger.info("MLflow model loaded successfully")
        
        scaler = load_scaler()
        logger.info("Scaler loaded successfully")
        
        # Store in app state
        app.state.model = model
        app.state.scaler = scaler
        
        async def check_model_updates():
            while True:
                await asyncio.sleep(300)
                reload_model_and_scaler(app)
        
        asyncio.create_task(check_model_updates())
        
    except Exception as e:
        logger.warning(f"Failed to load model from MLflow: {str(e)}")
        try:
            model = load_local_model()
            logger.info("Local model loaded successfully")
            app.state.model = model
        except Exception as local_e:
            logger.error(f"Failed to load local model: {str(local_e)}")
            app.state.model = None
            
        try:
            scaler = load_scaler()
            logger.info("Scaler loaded successfully")
            app.state.scaler = scaler
        except Exception as scaler_e:
            logger.error(f"Failed to load scaler: {str(scaler_e)}")
            app.state.scaler = None
    
    yield
    
    # Shutdown
    logger.info("Shutting down the application...")
    
    # Cleanup
    app.state.model = None
    app.state.scaler = None

app = FastAPI(
    title="FastAPI ML Document Processing",
    description="Document processing API with ML capabilities",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Setup Prometheus metrics
instrumentator = Instrumentator()
instrumentator.instrument(app).expose(app)
setup_metrics(app)

# Initialize services
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
model = GLiNER.from_pretrained("urchade/gliner_medium-v2.1")
labels = ["Name", "ID"]
embeddings = PineconeEmbeddings(model="multilingual-e5-large")
# pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])

# Main route
@app.get("/")
def read_root():
    return {"message": "FastAPI ML Document Processing API", "version": "1.0.0"}

# Health check endpoint
@app.get("/health")
def health_check():
    try:
        # Check MLflow connection
        mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
        client = MlflowClient()
        try:
            # Try to get experiment instead of list_experiments
            experiment = client.get_experiment_by_name("document-processing")
            mlflow_status = "connected" if experiment is not None else "connecting"
        except Exception as mlflow_error:
            logger.warning(f"MLflow connection issue: {str(mlflow_error)}")
            mlflow_status = "connecting"
        
        # Try to load model, but don't fail if not available yet
        try:
            model = load_best_model()
            model_status = "loaded"
        except Exception as model_error:
            logger.warning(f"Model not loaded yet: {str(model_error)}")
            model_status = "loading"
        
        # Check Supabase connection
        try:
            supabase.table("documents").select("count").limit(1).execute()
            supabase_status = "connected"
        except Exception as supabase_error:
            logger.warning(f"Supabase connection issue: {str(supabase_error)}")
            supabase_status = "connecting"
        
        return {
            "status": "healthy",
            "components": {
                "mlflow": mlflow_status,
                "model": model_status,
                "supabase": supabase_status
            }
        }
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return {
            "status": "unhealthy",
            "error": str(e),
            "components": {
                "mlflow": "error" if "mlflow" in str(e).lower() else "unknown",
                "model": "error" if "model" in str(e).lower() else "unknown",
                "supabase": "error" if "supabase" in str(e).lower() else "unknown"
            }
        }, 503

# Upload route
@app.post("/upload")
async def upload_file(uuid: str = Form(...), file_url: str = Form(...)):
    with mlflow.start_run(run_name=f"document_processing_{uuid}"):
        try:
            # Log parameters
            mlflow.log_param("uuid", uuid)
            mlflow.log_param("file_url", file_url)

            # Parse PDF
            loader = PyPDFLoader(file_url)
            documents = loader.load()
            page_count = len(documents)
            full_text = " ".join(doc.page_content for doc in documents)
            sentence_count = len([s for s in re.split(r'[.!?]+', full_text) if s.strip()])
            
            # Log basic metrics
            mlflow.log_metric("page_count", page_count)
            mlflow.log_metric("sentence_count", sentence_count)

            # Chunking and embedding
            text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=0)
            chunks = text_splitter.split_documents(documents)
            markdown_content = "\n\n".join(doc.page_content for doc in chunks)
            vector = embeddings.embed_documents([markdown_content])[0]

            # Entity extraction
            first_chunk = chunks[0].page_content if chunks else ""
            entities = model.predict_entities(first_chunk, labels)
            extracted_data = {"Name": "", "ID": ""}
            for entity in entities:
                extracted_data[entity["label"]] = entity["text"]

            # Get current record
            current_record = supabase.table("documents").select("*").eq("id", uuid).execute()
            if not current_record.data:
                raise HTTPException(status_code=404, detail=f"No record found with uuid: {uuid}")
            
            folder = current_record.data[0]["folder"]
            uploaded_date = current_record.data[0]["uploadedDate"]
            deadline = current_record.data[0]["deadline"]
            previous_records = supabase.table("documents").select("*").eq("folder", folder).lt("uploadedDate", uploaded_date).execute().data
            plagiarism_results = {}

            # Plagiarism detection
            if previous_records:
                previous_embeddings = []
                for record in previous_records:
                    try:
                        embedding = (
                            json.loads(record["embedding"]) if isinstance(record["embedding"], str)
                            else record["embedding"]
                        )
                        previous_embeddings.append([float(x) for x in embedding])
                    except Exception:
                        continue

                if previous_embeddings:
                    current_embedding = vector if isinstance(vector, list) else [float(x) for x in json.loads(vector)]
                    similarities = cosine_similarity(np.array([current_embedding]), np.array(previous_embeddings))[0]
                    similarity_list = [
                        (r["nameStudent"] or "Unknown", float(sim) if isinstance(sim, (int, float)) else 0.0)
                        for r, sim in zip([r for r in previous_records if r.get("embedding")], similarities)
                    ]
                    top_2 = sorted(similarity_list, key=lambda x: x[1], reverse=True)[:2]
                    plagiarism_results = dict(top_2)

            plagiarism_score = max(plagiarism_results.values()) if plagiarism_results else 0.0
            mlflow.log_metric("plagiarism_score", plagiarism_score)

            # Time difference
            deadline_dt = datetime.fromisoformat(deadline)
            uploaded_date_dt = datetime.fromisoformat(uploaded_date)
            time_diff = (deadline_dt - uploaded_date_dt).total_seconds() / 3600

            # Clustering
            df = pd.DataFrame({
                'sentences': [sentence_count],
                'page': [page_count],
                'timing': [time_diff],
                'plagiarism': [plagiarism_score]
            })
            scaled_data = StandardScaler().fit_transform(df)
            cluster = model.fit_predict(scaled_data)[0]

            # Update DB
            response = supabase.table("documents").update({
                "nameStudent": extracted_data["Name"] or "null",
                "NRP": extracted_data["ID"],
                "isiTugas": markdown_content,
                "embedding": vector.tolist() if isinstance(vector, np.ndarray) else vector,
                "page": page_count,
                "sentences": sentence_count,
                "plagiarism": plagiarism_results,
                "clustering": float(cluster)
            }).eq("id", uuid).execute()

            if not response.data:
                raise HTTPException(status_code=404, detail=f"No record found with uuid: {uuid}")

            return {
                "message": "File processed and record updated successfully.",
                "extracted_entities": extracted_data,
                "vector": vector,
                "page_count": page_count,
                "sentence_count": sentence_count,
                "plagiarism_results": plagiarism_results
            }

        except Exception as e:
            mlflow.log_param("error", str(e))
            raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

# Route baru untuk memproses feedback assignment
@app.post("/agent-feedback")
async def agent_feedback_endpoint(uuid: str = Form(...)):
    with mlflow.start_run(run_name=f"agent_feedback_{uuid}"):
        try:
            mlflow.log_param("uuid", uuid)
            
            # Get document data
            current_record = supabase.table("documents").select("*").eq("id", uuid).execute()
            if not current_record.data:
                raise HTTPException(status_code=404, detail=f"No record found with uuid: {uuid}")
            
            assignment_content = current_record.data[0]["isiTugas"]
            folder = current_record.data[0]["folder"]

            # Get folder information
            folder_record = supabase.table("folders").select("nameAssignment", "description").eq("nameAssignment", folder).execute()
            if not folder_record.data:
                raise HTTPException(status_code=404, detail=f"No folder found with nameAssignment: {folder}")

            title = folder_record.data[0]["nameAssignment"]
            description = folder_record.data[0]["description"]

            # Log parameters
            mlflow.log_param("title", title)
            mlflow.log_param("content_length", len(assignment_content))

            # Generate feedback
            persona = "Provide feedback in a formal and constructive tone suitable for academic purposes."
            payload = AssignmentRequest(
                assignment_meta={
                    "title": title,
                    "description": description
                },
                assignment_content=assignment_content
            )

            feedback_result = generate_feedback(payload)
            
            # Log metrics
            mlflow.log_metric("feedback_generated", 1)
            
            return feedback_result

        except Exception as e:
            mlflow.log_param("error", str(e))
            raise HTTPException(status_code=500, detail=str(e))

@app.post("/predict", response_model=PredictionOutput)
def predict(input_data: PredictionInput):
    try:
        # Get model and scaler from app state
        model = app.state.model
        scaler = app.state.scaler
        
        if model is None or scaler is None:
            raise HTTPException(status_code=503, detail="Model or scaler not loaded")
        
        # Create input array
        X = np.array([[input_data.sentences, input_data.page, input_data.timing, input_data.plagiarism]])
        print("Input data:", X)
        
        # Scale data using the saved scaler
        X_scaled = scaler.transform(X)
        print("Scaled data:", X_scaled)
        
        # Apply feature weights
        weights = np.array([0.5, 0.5, 1.5, 4.5])
        X_weighted = X_scaled * weights
        print("Weighted data:", X_weighted)
        
        # Make prediction
        cluster = int(model.predict(X_weighted)[0])
        print("Predicted cluster:", cluster)
        
        # Calculate confidence (distance to cluster center)
        if hasattr(model, 'cluster_centers_'):
            centers = model.cluster_centers_
            distances = np.linalg.norm(X_weighted - centers[cluster])
            confidence = float(1 / (1 + distances))  # Convert distance to confidence score
        else:
            confidence = 1.0
            
        # Get model metadata
        metadata = {
            "model_type": type(model).__name__,
            "n_clusters": getattr(model, 'n_clusters_', getattr(model, 'n_components_', None)),
            "feature_weights": weights.tolist(),
            "scaling": {
                "type": "min-max",
                "scaler_path": "/mlflow/artifacts/scaler.pkl"
            }
        }
        
        return PredictionOutput(
            cluster=cluster,
            confidence=confidence,
            metadata=metadata
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Prediction error: {str(e)}")

@app.get("/model-info")
def get_model_info():
    try:
        model = load_best_model()
        metadata = {
            "model_type": type(model).__name__,
            "n_clusters": getattr(model, 'n_clusters_', getattr(model, 'n_components_', None)),
            "feature_weights": [0.5, 0.5, 1.5, 4.5],
            "scaling": {
                "type": "min-max",
                "min_values": [0, 0, 0, 0],
                "max_values": [100, 100, 100, 100]
            }
        }
        return metadata
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting model info: {str(e)}")