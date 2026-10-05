import os
import uuid
import requests
from requests.adapters import HTTPAdapter
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from django.core.cache import cache

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from DashBoard.security import JWTAuthentication

load_dotenv()
DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL")

# Reusable HTTP Session with connection pooling for high performance
http_session = requests.Session()
adapter = HTTPAdapter(pool_connections=30, pool_maxsize=30)
http_session.mount("https://", adapter)
http_session.mount("http://", adapter)

def get_ist_now():
    """Returns current time in Indian Standard Time (IST)"""
    return datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()

def get_cached_pg_properties():
    """Helper to fetch and cache all PG properties for 30 seconds to speed up listing and filter APIs."""
    cache_key = "firebase_pg_properties_all"
    cached_data = cache.get(cache_key)
    if cached_data is not None:
        return cached_data
        
    url = f"{DATABASE_URL}/pg_properties.json"
    res = http_session.get(url, timeout=10)
    res.raise_for_status()
    data = res.json() or {}
    cache.set(cache_key, data, timeout=30)
    return data

class AddPgPropertyView(APIView):
    """
    POST, PUT, and GET APIs for PG properties in Firebase Realtime Database.
    Node Name: pg_properties
    """
    # Assuming this should be protected since it's an admin panel
    authentication_classes = [JWTAuthentication]


    def post(self, request):
        data = request.data

        # 1. Validate Required Fields
        required_fields = [
            "name", "pg_type", "living_type", "contact_person", "mobile",
            "address_line_1", "area", "city", "state", "pincode", "country",
            "no_of_rooms", "property_status"
        ]
        missing_fields = []
        for field in required_fields:
            val = data.get(field)
            if val is None or val == "":
                missing_fields.append(field)

        if missing_fields:
            return Response(
                {"detail": f"Missing required fields: {', '.join(missing_fields)}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not DATABASE_URL:
            return Response(
                {"detail": "Firebase database URL is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # 2. Build the payload
        # Auto-generate PG ID/Code (Sequential PG001, PG002, etc.)
        try:
            get_url = f"{DATABASE_URL}/pg_properties.json?shallow=true"
            response = http_session.get(get_url)
            response.raise_for_status()
            existing_pgs = response.json()
            
            next_num = 1
            if existing_pgs:
                numbers = []
                for key in existing_pgs.keys():
                    if key.startswith("PG"):
                        try:
                            num = int(key[2:])
                            numbers.append(num)
                        except ValueError:
                            pass
                if numbers:
                    next_num = max(numbers) + 1
                    
            pg_id = f"PG{next_num:03d}"
        except requests.exceptions.RequestException as e:
            return Response(
                {"detail": f"Failed to fetch from Firebase: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        # We explicitly map fields to ensure strict typing/schema matching
        payload = {
            "pg_id": pg_id,
            "name": str(data.get("name")),
            "pg_type": str(data.get("pg_type")),
            "living_type": str(data.get("living_type")),
            "contact_person": str(data.get("contact_person")),
            "mobile": str(data.get("mobile")),
            
            "address_line_1": str(data.get("address_line_1")),
            "area": str(data.get("area")),
            "city": str(data.get("city")),
            "state": str(data.get("state")),
            "pincode": str(data.get("pincode")),
            "country": str(data.get("country")),
            
            "no_of_rooms": int(data.get("no_of_rooms")),
            "property_status": bool(data.get("property_status")),
            
            "created_at": get_ist_now(),
        }

        # 3. Handle Optional Fields
        if "description" in data:
            payload["description"] = str(data.get("description"))
        if "address_line_2" in data:
            payload["address_line_2"] = str(data.get("address_line_2"))
        if "landmark" in data:
            payload["landmark"] = str(data.get("landmark"))
        if "amenities" in data:
            # amenities should be a list of strings
            amenities = data.get("amenities")
            if isinstance(amenities, list):
                payload["amenities"] = [str(item) for item in amenities]
            else:
                return Response(
                    {"detail": "amenities must be a list of strings."},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # 4. Handle Room Configuration
        pg_type = str(data.get("pg_type"))
        room_config = data.get("room_config")
        if room_config and isinstance(room_config, list):
            rooms_dict = {}
            for room in room_config:
                if pg_type == 'PG':
                    room_number = str(room.get("room_number", ""))
                    # Structured room_id
                    room_id = f"{pg_id}_R{room_number.replace(' ', '')}"
                    
                    try:
                        sharing = int(room.get("sharing", 1))
                    except ValueError:
                        sharing = 1
                        
                    try:
                        rent = int(room.get("rent", 0))
                    except ValueError:
                        rent = 0
                    
                    beds_dict = {}
                    # Auto-generate beds based on sharing value
                    for i in range(1, sharing + 1):
                        # Structured bed_id
                        bed_id = f"{room_id}_B{i}"
                        beds_dict[bed_id] = {
                            "bed_name": f"{room_number}-B{i}",
                            "is_occupied": False
                        }
                        
                    rooms_dict[room_id] = {
                        "room_number": room_number,
                        "sharing": sharing,
                        "rent": rent,
                        "beds": beds_dict
                    }
                elif pg_type == 'Apartment':
                    flat_no = str(room.get("flat_no", ""))
                    room_id = f"{pg_id}_F{flat_no.replace(' ', '')}"
                    
                    try:
                        bhk = int(room.get("bhk", 1))
                    except ValueError:
                        bhk = 1
                        
                    try:
                        rent = int(room.get("rent", 0))
                    except ValueError:
                        rent = 0
                        
                    rooms_dict[room_id] = {
                        "flat_no": flat_no,
                        "bhk": bhk,
                        "rent": rent
                    }
            payload["rooms"] = rooms_dict

        # 5. Save to Firebase
        url = f"{DATABASE_URL}/pg_properties/{pg_id}.json"
        
        try:
            # Using PUT to write at the specific node id
            response = http_session.put(url, json=payload)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            return Response(
                {"detail": f"Failed to save to Firebase: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response({
            "message": "PG property added successfully",
            "pg_id": pg_id,
            "data": payload
        }, status=status.HTTP_201_CREATED)

    def put(self, request):
        data = request.data
        
        pg_id = data.get("pg_id")
        if not pg_id:
            return Response(
                {"detail": "pg_id is required for updating."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not DATABASE_URL:
            return Response(
                {"detail": "Firebase database URL is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # Build payload for update (partial update using PATCH for firebase)
        payload = {}
        
        # Mapping allowed fields for update
        basic_fields = [
            "name", "pg_type", "living_type", "contact_person", "mobile",
            "address_line_1", "address_line_2", "area", "city", "state", 
            "pincode", "country", "landmark", "description"
        ]
        
        for field in basic_fields:
            if field in data:
                payload[field] = str(data.get(field))

        if "no_of_rooms" in data:
            try:
                payload["no_of_rooms"] = int(data.get("no_of_rooms"))
            except ValueError:
                pass
                
        if "property_status" in data:
            payload["property_status"] = bool(data.get("property_status"))
            
        if "amenities" in data:
            amenities = data.get("amenities")
            if isinstance(amenities, list):
                payload["amenities"] = [str(item) for item in amenities]
            else:
                return Response(
                    {"detail": "amenities must be a list of strings."},
                    status=status.HTTP_400_BAD_REQUEST
                )
                
        # Handle Room Configuration (Optional in Update)
        if "room_config" in data:
            # Fetch existing pg_type if not provided in payload to correctly format rooms
            pg_type = str(data.get("pg_type", payload.get("pg_type", "")))
            if not pg_type:
                try:
                    res = http_session.get(f"{DATABASE_URL}/pg_properties/{pg_id}/pg_type.json")
                    if res.status_code == 200 and res.json():
                        pg_type = res.json()
                except:
                    pg_type = "PG" # fallback

            room_config = data.get("room_config")
            if isinstance(room_config, list):
                rooms_dict = {}
                for room in room_config:
                    if pg_type == 'PG':
                        room_number = str(room.get("room_number", ""))
                        room_id = f"{pg_id}_R{room_number.replace(' ', '')}"
                        
                        try:
                            sharing = int(room.get("sharing", 1))
                        except ValueError:
                            sharing = 1
                            
                        try:
                            rent = int(room.get("rent", 0))
                        except ValueError:
                            rent = 0
                        
                        beds_dict = {}
                        # Note: This will overwrite existing bed occupancy statuses if updated.
                        for i in range(1, sharing + 1):
                            bed_id = f"{room_id}_B{i}"
                            beds_dict[bed_id] = {
                                "bed_name": f"{room_number}-B{i}",
                                "is_occupied": False
                            }
                            
                        rooms_dict[room_id] = {
                            "room_number": room_number,
                            "sharing": sharing,
                            "rent": rent,
                            "beds": beds_dict
                        }
                    elif pg_type == 'Apartment':
                        flat_no = str(room.get("flat_no", ""))
                        room_id = f"{pg_id}_F{flat_no.replace(' ', '')}"
                        
                        try:
                            bhk = int(room.get("bhk", 1))
                        except ValueError:
                            bhk = 1
                            
                        try:
                            rent = int(room.get("rent", 0))
                        except ValueError:
                            rent = 0
                            
                        rooms_dict[room_id] = {
                            "flat_no": flat_no,
                            "bhk": bhk,
                            "rent": rent
                        }
                payload["rooms"] = rooms_dict

        if not payload:
            return Response(
                {"detail": "No valid fields provided for update."},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        payload["updated_at"] = get_ist_now()

        # Save to Firebase using PATCH to partially update existing fields
        url = f"{DATABASE_URL}/pg_properties/{pg_id}.json"
        
        try:
            response = http_session.patch(url, json=payload)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            return Response(
                {"detail": f"Failed to update Firebase: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response({
            "message": "PG property updated successfully",
            "pg_id": pg_id,
            "updated_data": payload
        }, status=status.HTTP_200_OK)

    def get(self, request):
        if not DATABASE_URL:
            return Response(
                {"detail": "Firebase database URL is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
            
        # Allow checking pg_id in query params OR headers
        pg_id = request.GET.get("pg_id") or request.headers.get("pg_id") or request.headers.get("Pg-Id")
        
        if pg_id:
            url = f"{DATABASE_URL}/pg_properties/{pg_id}.json"
            try:
                response = http_session.get(url)
                response.raise_for_status()
                data = response.json()
                
                if not data:
                    return Response({"detail": "PG property not found."}, status=status.HTTP_404_NOT_FOUND)
                
                if "pg_id" not in data:
                    data["pg_id"] = pg_id
                    
                return Response(data, status=status.HTTP_200_OK)
                
            except requests.exceptions.RequestException as e:
                return Response(
                    {"detail": f"Failed to fetch from Firebase: {str(e)}"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
        try:
            data = get_cached_pg_properties()
            
            if not data:
                return Response({
                    "summary": {"total": 0, "active": 0, "inactive": 0},
                    "pagination": {"current_page": 1, "total_pages": 1, "has_next": False, "has_prev": False},
                    "data": []
                }, status=status.HTTP_200_OK)
                
            formatted_data = []
            active_count = 0
            inactive_count = 0
            
            # Get filter parameters
            f_property_type = request.GET.get("property_type") or request.headers.get("property_type")
            f_living_type = request.GET.get("living_type") or request.headers.get("living_type")
            f_property_status = request.GET.get("property_status") or request.headers.get("property_status")
            f_city = request.GET.get("city") or request.headers.get("city")
            f_state = request.GET.get("state") or request.headers.get("state")
            f_search = request.GET.get("search") or request.headers.get("search")
            
            for key, pg in data.items():
                if isinstance(pg, dict):
                    status_val = "active" if pg.get("property_status") else "inactive"
                    
                    # Apply filters
                    if f_property_type and pg.get("pg_type") != f_property_type:
                        continue
                    if f_living_type and pg.get("living_type") != f_living_type:
                        continue
                    if f_property_status and status_val != f_property_status.lower():
                        continue
                    if f_city and pg.get("city", "").lower() != f_city.lower():
                        continue
                    if f_state and pg.get("state", "").lower() != f_state.lower():
                        continue
                        
                    # Apply search (matches pg_id, name, or contact_person)
                    if f_search:
                        search_term = f_search.lower()
                        pg_id_val = str(pg.get("pg_id", key)).lower()
                        name_val = str(pg.get("name", "")).lower()
                        contact_val = str(pg.get("contact_person", "")).lower()
                        
                        if search_term not in pg_id_val and search_term not in name_val and search_term not in contact_val:
                            continue
                        
                    if status_val == "active":
                        active_count += 1
                    else:
                        inactive_count += 1
                        
                    formatted_data.append({
                        "pg_id": pg.get("pg_id", key),
                        "pg_name": pg.get("name", ""),
                        "pg_type": pg.get("pg_type", ""),
                        "living_type": pg.get("living_type", ""),
                        "contact_person": pg.get("contact_person", ""),
                        "mobile": pg.get("mobile", ""),
                        "status": status_val
                    })
            
            # Pagination
            try:
                page = int(request.GET.get("page", 1))
            except ValueError:
                page = 1
                
            try:
                limit = int(request.GET.get("limit", 10))
            except ValueError:
                limit = 10
                
            total_items = len(formatted_data)
            total_pages = (total_items + limit - 1) // limit
            
            start_idx = (page - 1) * limit
            end_idx = start_idx + limit
            paginated_data = formatted_data[start_idx:end_idx]
                    
            return Response({
                "summary": {
                    "total": total_items,
                    "active": active_count,
                    "inactive": inactive_count
                },
                "pagination": {
                    "current_page": page,
                    "limit": limit,
                    "total_pages": total_pages,
                    "total_items": total_items,
                    "has_next": page < total_pages,
                    "has_prev": page > 1
                },
                "data": paginated_data
            }, status=status.HTTP_200_OK)
            
        except requests.exceptions.RequestException as e:
            return Response(
                {"detail": f"Failed to fetch from Firebase: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def delete(self, request):
        if not DATABASE_URL:
            return Response(
                {"detail": "Firebase database URL is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        pg_id = request.data.get("pg_id") or request.GET.get("pg_id") or request.headers.get("pg_id")
        if not pg_id:
            return Response({"detail": "pg_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        url = f"{DATABASE_URL}/pg_properties/{pg_id}.json"
        
        try:
            # First, check if it exists
            check_response = http_session.get(url)
            check_response.raise_for_status()
            if not check_response.json():
                return Response({"detail": "PG property not found."}, status=status.HTTP_404_NOT_FOUND)

            # Proceed to soft delete (change status to inactive)
            response = http_session.patch(url, json={"property_status": False})
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            return Response(
                {"detail": f"Failed to soft delete from Firebase: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response({"message": f"PG property {pg_id} softly deleted (marked inactive) successfully"}, status=status.HTTP_200_OK)


class GetStatesView(APIView):
    """
    GET API to fetch a list of all unique states from the properties.
    """
    authentication_classes = [JWTAuthentication]

    def get(self, request):
        if not DATABASE_URL:
            return Response(
                {"detail": "Firebase database URL is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
            
        url = f"{DATABASE_URL}/pg_properties.json"
        
        try:
            response = http_session.get(url)
            response.raise_for_status()
            data = response.json()
            
            if not data:
                return Response([], status=status.HTTP_200_OK)
                
            states = set()
            for key, pg in data.items():
                if isinstance(pg, dict):
                    state = pg.get("state")
                    if state:
                        states.add(state.strip())
                        
            # Return sorted list of unique states
            return Response(sorted(list(states)), status=status.HTTP_200_OK)
            
        except requests.exceptions.RequestException as e:
            return Response(
                {"detail": f"Failed to fetch from Firebase: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class GetCitiesView(APIView):
    """
    GET API to fetch a list of all unique cities from the properties.
    Optionally filter by state (e.g., ?state=Karnataka).
    """
    authentication_classes = [JWTAuthentication]

    def get(self, request):
        if not DATABASE_URL:
            return Response(
                {"detail": "Firebase database URL is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
            
        # Optional state filter
        f_state = request.GET.get("state") or request.headers.get("state")
        
        try:
            data = get_cached_pg_properties()
            
            if not data:
                return Response([], status=status.HTTP_200_OK)
                
            cities = set()
            for key, pg in data.items():
                if isinstance(pg, dict):
                    # Filter by state if provided
                    if f_state and pg.get("state", "").lower() != f_state.lower():
                        continue
                        
                    city = pg.get("city")
                    if city:
                        cities.add(city.strip())
                        
            # Return sorted list of unique cities
            return Response(sorted(list(cities)), status=status.HTTP_200_OK)
            
        except requests.exceptions.RequestException as e:
            return Response(
                {"detail": f"Failed to fetch from Firebase: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
