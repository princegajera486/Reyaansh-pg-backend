import os
import cloudinary
import cloudinary.uploader
import cloudinary.api
from dotenv import load_dotenv

load_dotenv()

# Configure Cloudinary
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)

def upload_image(file, folder="screenshots"):
    """
    Uploads an image file (e.g., screenshot) to Cloudinary.
    Returns the secure URL of the uploaded image or None on failure.
    """
    try:
        response = cloudinary.uploader.upload(
            file,
            folder=folder,
            resource_type="image"
        )
        return response.get("secure_url")
    except Exception as e:
        print(f"Cloudinary image upload failed: {e}")
        return None

def upload_image_full(file, folder="members"):
    """
    Uploads an image file (e.g., member photo, aadhaar card) to Cloudinary.
    Returns dict with 'public_id' and 'secure_url' or None on failure.
    """
    try:
        response = cloudinary.uploader.upload(
            file,
            folder=folder,
            resource_type="image"
        )
        return {
            "public_id": response.get("public_id"),
            "secure_url": response.get("secure_url"),
            "url": response.get("url")
        }
    except Exception as e:
        print(f"Cloudinary image upload failed: {e}")
        return None

def upload_pdf(file, folder="pdfs"):
    """
    Uploads a PDF document to Cloudinary.
    Returns the secure URL of the uploaded document or None on failure.
    """
    try:
        response = cloudinary.uploader.upload(
            file,
            folder=folder,
            resource_type="raw" # PDFs are typically uploaded as 'raw' or 'image' (for thumbnailing)
        )
        return response.get("secure_url")
    except Exception as e:
        print(f"Cloudinary PDF upload failed: {e}")
        return None

def delete_file(public_id, resource_type="image"):
    """
    Deletes a file from Cloudinary given its public_id.
    """
    try:
        response = cloudinary.uploader.destroy(public_id, resource_type=resource_type)
        return response.get("result") == "ok"
    except Exception as e:
        print(f"Cloudinary delete failed: {e}")
        return False
