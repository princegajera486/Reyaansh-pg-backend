import os
import requests
from requests.adapters import HTTPAdapter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from django.core.cache import cache

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from DashBoard.cloudinary_client import upload_image_full


load_dotenv()
DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL")

# Reusable HTTP Session with connection pooling for fast Firebase API performance
http_session = requests.Session()
adapter = HTTPAdapter(pool_connections=30, pool_maxsize=30)
http_session.mount("https://", adapter)
http_session.mount("http://", adapter)

def process_cloudinary_image(raw_val, folder="members"):
    """
    Uploads an image file object, base64 string, or remote image URL to Cloudinary.
    Returns tuple of (cloudinary_public_id, cloudinary_secure_url).
    """
    if not raw_val:
        return None, None
    try:
        # Check if it's an uploaded file object or base64 data string
        if hasattr(raw_val, 'read') or hasattr(raw_val, 'chunks') or (isinstance(raw_val, str) and (raw_val.startswith("data:image/") or len(raw_val) > 500)):
            res = upload_image_full(raw_val, folder=folder)
            if res and res.get("public_id"):
                return res["public_id"], res["secure_url"]
            return None, None
        elif isinstance(raw_val, str):
            val_str = raw_val.strip()
            if not val_str:
                return None, None
            if val_str.startswith("http://") or val_str.startswith("https://"):
                # Upload remote image to Cloudinary to generate a Cloudinary public_id
                res = upload_image_full(val_str, folder=folder)
                if res and res.get("public_id"):
                    return res["public_id"], res["secure_url"]
                return val_str, val_str
            else:
                # It is already a Cloudinary public_id
                cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "")
                secure_url = f"https://res.cloudinary.com/{cloud_name}/image/upload/{val_str}" if cloud_name else val_str
                return val_str, secure_url
    except Exception as e:
        print(f"Cloudinary image processing error: {e}")
        return None, None
    return None, None

def fetch_nodes_parallel(url_dict):
    """
    Fetches multiple Firebase URLs concurrently using thread pool.
    """
    def fetch_one(item):
        key, url = item
        
        # Check cache first
        cache_key = f"firebase_{url}"
        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return key, cached_data
            
        try:
            res = http_session.get(url, timeout=10)
            if res.status_code == 200 and res.json():
                data = res.json()
                # Cache for 30 seconds
                cache.set(cache_key, data, timeout=30)
                return key, data
        except Exception:
            pass
        return key, {}

    with ThreadPoolExecutor(max_workers=max(1, len(url_dict))) as executor:
        results = dict(executor.map(fetch_one, url_dict.items()))
    return results

def get_ist_now():
    """Returns current time in Indian Standard Time (IST)"""
    return datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()

class MemberView(APIView):
    """
    POST/PUT/DELETE/GET API to manage a member in Firebase Realtime Database.
    Node Name: members
    """

    def get(self, request):
        if not DATABASE_URL:
            return Response(
                {"detail": "Firebase database URL is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        member_id = request.query_params.get("member_id") or request.query_params.get("id")
        
        try:
            fetch_dict = {
                "rent": f"{DATABASE_URL}/rent_records.json",
                "pgs": f"{DATABASE_URL}/pg_properties.json"
            }
            if not member_id:
                fetch_dict["members"] = f"{DATABASE_URL}/members.json"

            # Parallel fetch
            nodes = fetch_nodes_parallel(fetch_dict)
            rent_records = nodes["rent"]
            pg_properties = nodes["pgs"]

            # --- SINGLE MEMBER FULL DETAILS ---
            if member_id:
                url = f"{DATABASE_URL}/members/{member_id}.json"
                res = http_session.get(url)
                res.raise_for_status()
                member_data = res.json()
                
                if not member_data or member_data.get("is_deleted"):
                    return Response({"detail": "Member not found."}, status=status.HTTP_404_NOT_FOUND)
                
                member_data["member_id"] = member_id
                
                pg_id = member_data.get("pg_id", "")
                room_id = member_data.get("room_id", "")
                bed_id = member_data.get("bed_id", "")
                
                member_data["pg_name"] = pg_id
                member_data["room_number"] = room_id
                member_data["bed_name"] = bed_id
                
                if pg_id and pg_id in pg_properties:
                    pg_info = pg_properties[pg_id]
                    member_data["pg_name"] = pg_info.get("property_name", pg_info.get("pg_name", pg_info.get("name", pg_id)))
                    
                    if room_id and "rooms" in pg_info and room_id in pg_info["rooms"]:
                        room_info = pg_info["rooms"][room_id]
                        member_data["room_number"] = room_info.get("room_number", room_info.get("room_name", room_info.get("name", room_id)))
                        
                        if bed_id and "beds" in room_info and bed_id in room_info["beds"]:
                            bed_info = room_info["beds"][bed_id]
                            if isinstance(bed_info, dict):
                                member_data["bed_name"] = bed_info.get("bed_number", bed_info.get("bed_name", bed_info.get("name", bed_id)))
                
                member_rent_record = rent_records.get(member_id, {})
                if isinstance(member_rent_record, dict) and member_rent_record.get("status"):
                    member_data["rent_status"] = member_rent_record.get("status")
                else:
                    member_data["rent_status"] = "Pending"

                return Response({
                    "message": "Member details fetched successfully",
                    "data": member_data
                }, status=status.HTTP_200_OK)

            # Search & Filter Parameters (Query Params or Headers)
            search_param = request.query_params.get("search") or request.headers.get("search")
            member_name_param = request.query_params.get("member_name") or request.query_params.get("name") or request.headers.get("member_name")
            pg_name_param = request.query_params.get("pg_name") or request.headers.get("pg_name")
            gender_param = request.query_params.get("gender") or request.headers.get("gender")
            member_status_param = request.query_params.get("member_status") or request.query_params.get("status") or request.headers.get("member_status")
            rent_status_param = request.query_params.get("rent_status") or request.headers.get("rent_status")
            joining_date_param = request.query_params.get("joining_date") or request.query_params.get("created_at") or request.headers.get("joining_date")

            # --- ALL MEMBERS (PAGINATED & FILTERED) ---
            members_dict = nodes.get("members", {})

            formatted_members = []

            for m_id, m_data in members_dict.items():
                if not m_data or m_data.get("is_deleted"):
                    continue
                    
                rent_status = "Pending"
                member_rent_record = rent_records.get(m_id, {})
                if isinstance(member_rent_record, dict) and member_rent_record.get("status"):
                    rent_status = member_rent_record.get("status")

                pg_id = m_data.get("pg_id", "")
                room_id = m_data.get("room_id", "")
                bed_id = m_data.get("bed_id", "")
                
                pg_name = pg_id
                room_name = room_id
                bed_name = bed_id
                
                if pg_id and pg_id in pg_properties:
                    pg_info = pg_properties[pg_id]
                    pg_name = pg_info.get("property_name", pg_info.get("pg_name", pg_info.get("name", pg_id)))
                    
                    if room_id and "rooms" in pg_info and room_id in pg_info["rooms"]:
                        room_info = pg_info["rooms"][room_id]
                        room_name = room_info.get("room_number", room_info.get("room_name", room_info.get("name", room_id)))
                        
                        if bed_id and "beds" in room_info and bed_id in room_info["beds"]:
                            bed_info = room_info["beds"][bed_id]
                            if isinstance(bed_info, dict):
                                bed_name = bed_info.get("bed_number", bed_info.get("bed_name", bed_info.get("name", bed_id)))

                m_name = m_data.get("full_name", m_data.get("name", ""))
                m_gender = m_data.get("gender", "")
                m_status = m_data.get("status", "")
                m_joining_date = m_data.get("joining_date", m_data.get("created_at", ""))

                # Apply search filter (matches member name or pg name)
                if search_param:
                    s_lower = search_param.lower()
                    if s_lower not in m_name.lower() and s_lower not in str(pg_name).lower():
                        continue

                # Apply member_name filter
                if member_name_param and member_name_param.lower() not in m_name.lower():
                    continue

                # Apply pg_name filter
                if pg_name_param and pg_name_param.lower() not in str(pg_name).lower():
                    continue

                # Apply gender filter
                if gender_param and m_gender.lower() != gender_param.lower():
                    continue

                # Apply member_status filter
                if member_status_param and m_status.lower() != member_status_param.lower():
                    continue

                # Apply rent_status filter
                if rent_status_param and rent_status.lower() != rent_status_param.lower():
                    continue

                # Apply joining_date filter
                if joining_date_param and joining_date_param.lower() not in str(m_joining_date).lower():
                    continue

                formatted_members.append({
                    "id": m_id,
                    "name": m_name,
                    "mobile": m_data.get("mobile_number", ""),
                    "pg_name": pg_name,
                    "room_number": room_name,
                    "bed_name": bed_name,
                    "monthly_rent": m_data.get("monthly_rent", 0),
                    "due_date": m_data.get("rent_due_date", ""),
                    "rent_status": rent_status,
                    "member_status": m_status,
                    "gender": m_gender,
                    "joining_date": m_joining_date
                })

            formatted_members.reverse()

            # Pagination
            try:
                page = max(1, int(request.query_params.get("page", 1)))
            except (ValueError, TypeError):
                page = 1

            try:
                page_size = max(1, int(request.query_params.get("page_size", 10)))
            except (ValueError, TypeError):
                page_size = 10

            total_records = len(formatted_members)
            total_pages = (total_records + page_size - 1) // page_size if total_records > 0 else 1

            start_index = (page - 1) * page_size
            end_index = start_index + page_size
            paginated_data = formatted_members[start_index:end_index]

            return Response({
                "message": "Members fetched successfully",
                "pagination": {
                    "total_records": total_records,
                    "total_pages": total_pages,
                    "current_page": page,
                    "page_size": page_size,
                    "has_next": page < total_pages,
                    "has_previous": page > 1
                },
                "data": paginated_data
            }, status=status.HTTP_200_OK)

        except requests.exceptions.RequestException as e:
            return Response(
                {"detail": f"Failed to fetch members: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def post(self, request):
        data = request.data
        files = request.FILES

        if not DATABASE_URL:
            return Response(
                {"detail": "Firebase database URL is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # Extract mandatory images (member_img and aadhaar_card_img) from request FILES or data
        member_img_raw = (
            files.get("member_img") or files.get("member_image") or files.get("profile_img") or files.get("profile_image") or files.get("member_photo") or
            data.get("member_img") or data.get("member_image") or data.get("profile_img") or data.get("profile_image") or data.get("member_photo")
        )

        aadhaar_img_raw = (
            files.get("aadhaar_card_img") or files.get("aadhaar_img") or files.get("aadhaar_card_image") or files.get("aadhaar_card") or files.get("aadhar_card_img") or files.get("aadhar_img") or
            data.get("aadhaar_card_img") or data.get("aadhaar_img") or data.get("aadhaar_card_image") or data.get("aadhaar_card") or data.get("aadhar_card_img") or data.get("aadhar_img")
        )

        # 1. Validate Required Fields
        required_fields = [
            "full_name", "mobile_number", "occupation", "dob", "gender", "company_college_name",
            "aadhaar_number",
            "emergency_contact_name", "emergency_contact_relationship", "emergency_contact_number",
            "address_line_1", "country", "state", "city", "pincode",
            "pg_type", "pg_id", "room_id",
            "monthly_rent", "security_deposit", "maintenance_charge", "rent_due_date", "notice_period_days",
            "status"
        ]
        
        missing_fields = [field for field in required_fields if data.get(field) in [None, ""]]
        
        if not member_img_raw:
            missing_fields.append("member_img")
        if not aadhaar_img_raw:
            missing_fields.append("aadhaar_card_img")

        pg_type = str(data.get("pg_type", ""))
        status_val = str(data.get("status", ""))
        
        # Conditional required fields
        if pg_type == 'PG' and not data.get("bed_id"):
            missing_fields.append("bed_id")
            
        if status_val == 'Notice Period' and not data.get("status_reason"):
            missing_fields.append("status_reason")

        if missing_fields:
            return Response(
                {"detail": f"Missing required fields: {', '.join(missing_fields)}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Upload mandatory images to Cloudinary
        member_img_id, member_img_url = process_cloudinary_image(member_img_raw, folder="members/profile_images")
        if not member_img_id:
            return Response(
                {"detail": "Failed to upload member_img to Cloudinary. Please provide a valid image file, base64 string, or Cloudinary ID."},
                status=status.HTTP_400_BAD_REQUEST
            )

        aadhaar_img_id, aadhaar_img_url = process_cloudinary_image(aadhaar_img_raw, folder="members/aadhaar_cards")
        if not aadhaar_img_id:
            return Response(
                {"detail": "Failed to upload aadhaar_card_img to Cloudinary. Please provide a valid image file, base64 string, or Cloudinary ID."},
                status=status.HTTP_400_BAD_REQUEST
            )

        pg_id = str(data.get("pg_id"))
        room_id = str(data.get("room_id"))
        bed_id = str(data.get("bed_id")) if pg_type == 'PG' else None

        # 2. Check bed availability if it's a PG
        if pg_type == 'PG':
            bed_url = f"{DATABASE_URL}/pg_properties/{pg_id}/rooms/{room_id}/beds/{bed_id}.json"
            try:
                bed_res = http_session.get(bed_url)
                bed_res.raise_for_status()
                bed_data = bed_res.json()
                
                if not bed_data:
                    return Response({"detail": "The specified bed does not exist."}, status=status.HTTP_404_NOT_FOUND)
                
                if bed_data.get("is_occupied") is True:
                    return Response({"detail": "The specified bed is already occupied."}, status=status.HTTP_400_BAD_REQUEST)
                    
            except requests.exceptions.RequestException as e:
                return Response(
                    {"detail": f"Failed to verify bed availability: {str(e)}"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

        # 3. Build payload
        payload = {
            # Personal Info
            "full_name": str(data.get("full_name")),
            "mobile_number": str(data.get("mobile_number")),
            "occupation": str(data.get("occupation")),
            "dob": str(data.get("dob")),
            "gender": str(data.get("gender")),
            "company_college_name": str(data.get("company_college_name")),
            
            # Identity Verification & Images (Stored in Cloudinary, Cloudinary ID stored in Firebase)
            "aadhaar_number": str(data.get("aadhaar_number")),
            "member_img": member_img_id,
            "member_img_id": member_img_id,
            "member_img_url": member_img_url,
            "aadhaar_card_img": aadhaar_img_id,
            "aadhaar_card_img_id": aadhaar_img_id,
            "aadhaar_card_img_url": aadhaar_img_url,
            
            # Emergency Contact
            "emergency_contact_name": str(data.get("emergency_contact_name")),
            "emergency_contact_relationship": str(data.get("emergency_contact_relationship")),
            "emergency_contact_number": str(data.get("emergency_contact_number")),
            
            # Address Details
            "address_line_1": str(data.get("address_line_1")),
            "country": str(data.get("country")),
            "state": str(data.get("state")),
            "city": str(data.get("city")),
            "pincode": str(data.get("pincode")),
            
            # Stay Details
            "pg_type": pg_type,
            "pg_id": pg_id,
            "room_id": room_id,
            
            # Rent Details
            "monthly_rent": int(data.get("monthly_rent")),
            "security_deposit": int(data.get("security_deposit")),
            "maintenance_charge": int(data.get("maintenance_charge")),
            "rent_due_date": str(data.get("rent_due_date")),
            "notice_period_days": int(data.get("notice_period_days")),
            
            # Member Status
            "status": status_val,
            
            # Timestamps
            "created_at": get_ist_now(),
            "updated_at": get_ist_now(),
        }
        
        # Optional fields
        if "alternate_mobile_number" in data and data.get("alternate_mobile_number"):
            payload["alternate_mobile_number"] = str(data.get("alternate_mobile_number"))
        if "email" in data and data.get("email"):
            payload["email"] = str(data.get("email"))
        elif "email_id" in data and data.get("email_id"):
            payload["email"] = str(data.get("email_id"))
        elif "emailId" in data and data.get("emailId"):
            payload["email"] = str(data.get("emailId"))
        if False:
            payload["email"] = str(data.get("email"))
        if "pan_number" in data and data.get("pan_number"):
            payload["pan_number"] = str(data.get("pan_number"))
        if "driving_licence_number" in data and data.get("driving_licence_number"):
            payload["driving_licence_number"] = str(data.get("driving_licence_number"))
        if "address_line_2" in data and data.get("address_line_2"):
            payload["address_line_2"] = str(data.get("address_line_2"))
        if pg_type == 'PG' and bed_id:
            payload["bed_id"] = bed_id
        if status_val == 'Notice Period' and data.get("status_reason"):
            payload["status_reason"] = str(data.get("status_reason"))
            
        if "fcm_token" in data and data.get("fcm_token"):
            payload["fcm_token"] = str(data.get("fcm_token"))
        elif "fcmToken" in data and data.get("fcmToken"):
            payload["fcm_token"] = str(data.get("fcmToken"))

        if "device_code" in data and data.get("device_code"):
            payload["device_code"] = str(data.get("device_code"))
        elif "deviceCode" in data and data.get("deviceCode"):
            payload["device_code"] = str(data.get("deviceCode"))

        # 4. Generate Proper Member ID (MEM001, MEM002, etc.)
        try:
            get_url = f"{DATABASE_URL}/members.json?shallow=true"
            response = http_session.get(get_url)
            response.raise_for_status()
            existing_members = response.json()
            
            next_num = 1
            if existing_members:
                numbers = []
                for key in existing_members.keys():
                    if key.startswith("MEM"):
                        try:
                            num = int(key[3:])
                            numbers.append(num)
                        except ValueError:
                            pass
                if numbers:
                    next_num = max(numbers) + 1
                    
            member_id = f"MEM{next_num:03d}"
            payload["member_id"] = member_id
        except requests.exceptions.RequestException as e:
            return Response(
                {"detail": f"Failed to generate member ID: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # 5. Save to Firebase Members Node
        member_url = f"{DATABASE_URL}/members/{member_id}.json"
        try:
            member_res = http_session.put(member_url, json=payload)
            member_res.raise_for_status()
        except requests.exceptions.RequestException as e:
            return Response(
                {"detail": f"Failed to create member: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # 5. Update Bed Status if PG
        if pg_type == 'PG':
            try:
                # Update bed to occupied and map the member_id
                bed_patch = {
                    "is_occupied": True,
                    "member_id": member_id
                }
                http_session.patch(bed_url, json=bed_patch)
            except requests.exceptions.RequestException as e:
                # Note: This means member was created, but bed wasn't updated. 
                # A manual rollback or retry logic might be needed in a robust system.
                return Response(
                    {"detail": f"Member created, but failed to update bed status: {str(e)}"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

        # 6. Create Rent Record automatically (as mentioned: "Referenced in the rent_records node via member_id")
        rent_record_url = f"{DATABASE_URL}/rent_records/{member_id}.json"
        rent_payload = {
            "member_id": member_id,
            "pg_id": pg_id,
            "monthly_rent": payload["monthly_rent"],
            "security_deposit": payload["security_deposit"],
            "maintenance_charge": payload["maintenance_charge"],
            "rent_due_date": payload["rent_due_date"],
            "notice_period_days": payload["notice_period_days"],
            "created_at": payload["created_at"]
        }
        try:
            http_session.put(rent_record_url, json=rent_payload)
        except requests.exceptions.RequestException as e:
            # We silently ignore rent record creation failure or just log it.
            pass

        # Append member_id to response payload
        payload["member_id"] = member_id
        
        # 7. Auto-send Calendar Reminder ONLY to the newly added member if email is provided
        # Support optional send_email / send_welcome_email flag (defaults to True)
        send_email_option = data.get("send_email", data.get("send_welcome_email", data.get("sendEmail", True)))
        if isinstance(send_email_option, str):
            should_send_email = send_email_option.lower() not in ("false", "0", "no")
        else:
            should_send_email = bool(send_email_option)

        email = payload.get("email")
        if email and should_send_email:
            try:
                from DashBoard.views import SendCalendarReminderView
                class MockRequest:
                    def __init__(self, req_data):
                        self.data = req_data
                
                full_name = payload.get("full_name", "Member")
                mock_req = MockRequest({
                    "email": email,
                    "member_id": member_id,
                    "title": "Welcome! Monthly Rent Reminder",
                    "description": f"Welcome {full_name} to the PG! This is an automated calendar reminder for your monthly rent payment."
                })
                # Trigger background thread in calendar view ONLY for this specific new member
                SendCalendarReminderView().post(mock_req)
                print(f"Triggered welcome reminder email ONLY for new member {member_id} ({email})")
            except Exception as e:
                print(f"Failed to auto-send calendar reminder to new member {member_id}: {e}")
                

        return Response({
            "message": "Member created successfully",
            "member_id": member_id,
            "data": payload
        }, status=status.HTTP_201_CREATED)

    def put(self, request):
        return self.update_member(request)

    def patch(self, request):
        return self.update_member(request)

    def update_member(self, request):
        data = request.data
        member_id = data.get("member_id")

        if not DATABASE_URL:
            return Response(
                {"detail": "Firebase database URL is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        if not member_id:
            return Response(
                {"detail": "member_id is required for updating."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 1. Fetch existing member
        member_url = f"{DATABASE_URL}/members/{member_id}.json"
        try:
            member_res = http_session.get(member_url)
            member_res.raise_for_status()
            existing_member = member_res.json()
            
            if not existing_member:
                return Response(
                    {"detail": "Member not found."},
                    status=status.HTTP_404_NOT_FOUND
                )
        except requests.exceptions.RequestException as e:
            return Response(
                {"detail": f"Failed to fetch member details: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # Keep track of old bed for bed status update if it changes
        old_pg_type = existing_member.get("pg_type")
        old_pg_id = existing_member.get("pg_id")
        old_room_id = existing_member.get("room_id")
        old_bed_id = existing_member.get("bed_id")

        # 2. Update fields
        update_data = existing_member.copy()
        files = request.FILES
        
        for key, value in data.items():
            if key not in ["member_id", "created_at"] and value is not None and str(value).strip() != "":
                if isinstance(value, (int, bool, dict, list)):
                    update_data[key] = value
                else:
                    update_data[key] = str(value)
                    
        # Process image updates if provided
        member_img_raw = (
            files.get("member_img") or files.get("member_image") or files.get("profile_img") or files.get("profile_image") or files.get("member_photo") or
            data.get("member_img") or data.get("member_image") or data.get("profile_img") or data.get("profile_image") or data.get("member_photo")
        )

        aadhaar_img_raw = (
            files.get("aadhaar_card_img") or files.get("aadhaar_img") or files.get("aadhaar_card_image") or files.get("aadhaar_card") or files.get("aadhar_card_img") or files.get("aadhar_img") or
            data.get("aadhaar_card_img") or data.get("aadhaar_img") or data.get("aadhaar_card_image") or data.get("aadhaar_card") or data.get("aadhar_card_img") or data.get("aadhar_img")
        )

        if member_img_raw:
            m_id, m_url = process_cloudinary_image(member_img_raw, folder="members/profile_images")
            if m_id:
                update_data["member_img"] = m_id
                update_data["member_img_id"] = m_id
                update_data["member_img_url"] = m_url

        if aadhaar_img_raw:
            a_id, a_url = process_cloudinary_image(aadhaar_img_raw, folder="members/aadhaar_cards")
            if a_id:
                update_data["aadhaar_card_img"] = a_id
                update_data["aadhaar_card_img_id"] = a_id
                update_data["aadhaar_card_img_url"] = a_url

        update_data["updated_at"] = get_ist_now()

        new_pg_type = update_data.get("pg_type")
        new_pg_id = update_data.get("pg_id")
        new_room_id = update_data.get("room_id")
        new_bed_id = update_data.get("bed_id")

        bed_changed = False
        if new_pg_type == 'PG' and (old_pg_id != new_pg_id or old_room_id != new_room_id or old_bed_id != new_bed_id):
            bed_changed = True
            
            if new_bed_id:
                new_bed_url = f"{DATABASE_URL}/pg_properties/{new_pg_id}/rooms/{new_room_id}/beds/{new_bed_id}.json"
                try:
                    new_bed_res = http_session.get(new_bed_url)
                    new_bed_res.raise_for_status()
                    new_bed_data = new_bed_res.json()
                    
                    if not new_bed_data:
                        return Response({"detail": "The specified new bed does not exist."}, status=status.HTTP_404_NOT_FOUND)
                    
                    if new_bed_data.get("is_occupied") is True and str(new_bed_data.get("member_id", "")) != member_id:
                        return Response({"detail": "The specified new bed is already occupied."}, status=status.HTTP_400_BAD_REQUEST)
                except requests.exceptions.RequestException as e:
                    return Response(
                        {"detail": f"Failed to verify new bed availability: {str(e)}"},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )

        # 3. Save to Firebase Members Node
        try:
            update_res = http_session.put(member_url, json=update_data)
            update_res.raise_for_status()
        except requests.exceptions.RequestException as e:
            return Response(
                {"detail": f"Failed to update member: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # 4. Update Bed Status if PG
        if bed_changed:
            # Free old bed
            if old_pg_type == 'PG' and old_pg_id and old_room_id and old_bed_id:
                old_bed_url = f"{DATABASE_URL}/pg_properties/{old_pg_id}/rooms/{old_room_id}/beds/{old_bed_id}.json"
                try:
                    http_session.patch(old_bed_url, json={"is_occupied": False, "member_id": None})
                except:
                    pass
            
            # Occupy new bed
            if new_pg_type == 'PG' and new_pg_id and new_room_id and new_bed_id:
                try:
                    http_session.patch(new_bed_url, json={"is_occupied": True, "member_id": member_id})
                except:
                    pass

        # 5. Update rent records if rent details changed
        rent_fields = ["monthly_rent", "security_deposit", "maintenance_charge", "rent_due_date", "notice_period_days", "pg_id"]
        if any(key in data for key in rent_fields):
            rent_record_url = f"{DATABASE_URL}/rent_records/{member_id}.json"
            try:
                rent_res = http_session.get(rent_record_url)
                if rent_res.status_code == 200 and rent_res.json():
                    rent_payload = rent_res.json()
                    for field in rent_fields:
                        if field in update_data:
                            rent_payload[field] = update_data[field]
                    
                    http_session.put(rent_record_url, json=rent_payload)
            except:
                pass

        return Response({
            "message": "Member updated successfully",
            "member_id": member_id,
            "data": update_data
        }, status=status.HTTP_200_OK)

    def delete(self, request):
        member_id = request.query_params.get("member_id") or request.data.get("member_id")

        if not DATABASE_URL:
            return Response(
                {"detail": "Firebase database URL is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        if not member_id:
            return Response(
                {"detail": "member_id is required for deletion."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 1. Fetch existing member
        member_url = f"{DATABASE_URL}/members/{member_id}.json"
        try:
            member_res = http_session.get(member_url)
            member_res.raise_for_status()
            existing_member = member_res.json()
            
            if not existing_member:
                return Response(
                    {"detail": "Member not found."},
                    status=status.HTTP_404_NOT_FOUND
                )
        except requests.exceptions.RequestException as e:
            return Response(
                {"detail": f"Failed to fetch member details: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # 2. Free up the bed if occupied
        pg_type = existing_member.get("pg_type")
        pg_id = existing_member.get("pg_id")
        room_id = existing_member.get("room_id")
        bed_id = existing_member.get("bed_id")

        if pg_type == 'PG' and pg_id and room_id and bed_id:
            bed_url = f"{DATABASE_URL}/pg_properties/{pg_id}/rooms/{room_id}/beds/{bed_id}.json"
            try:
                http_session.patch(bed_url, json={"is_occupied": False, "member_id": None})
            except:
                pass # Silently ignore bed update failure for now

        # 3. Delete rent record
        rent_record_url = f"{DATABASE_URL}/rent_records/{member_id}.json"
        try:
            http_session.delete(rent_record_url)
        except:
            pass

        # 4. Soft Delete member
        try:
            patch_data = {
                "is_deleted": True,
                "status": "Deleted",
                "updated_at": get_ist_now()
            }
            delete_res = http_session.patch(member_url, json=patch_data)
            delete_res.raise_for_status()
        except requests.exceptions.RequestException as e:
            return Response(
                {"detail": f"Failed to delete member: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response({
            "message": "Member deleted successfully",
            "member_id": member_id
        }, status=status.HTTP_200_OK)


class PgAvailabilityView(APIView):
    """
    GET API to fetch active PGs with available rooms and beds.
    Endpoint in PgMembers for checking availability when adding/managing members.
    Supports filtering by pg_id, city, state, living_type, property_type, and search.
    """

    def get(self, request):
        if not DATABASE_URL:
            return Response(
                {"detail": "Firebase database URL is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        f_pg_id = request.GET.get("pg_id") or request.headers.get("pg_id")
        f_city = request.GET.get("city") or request.headers.get("city")
        f_state = request.GET.get("state") or request.headers.get("state")
        f_living_type = request.GET.get("living_type") or request.headers.get("living_type")
        f_property_type = request.GET.get("property_type") or request.query_params.get("pg_type") or request.headers.get("property_type")
        f_search = request.GET.get("search") or request.headers.get("search")

        url = f"{DATABASE_URL}/pg_properties.json"

        try:
            response = http_session.get(url)
            response.raise_for_status()
            data = response.json() or {}

            availability_list = []

            for key, pg in data.items():
                if not isinstance(pg, dict):
                    continue

                # 1. Must be an active property
                property_status = pg.get("property_status")
                is_active = property_status is True or str(property_status).lower() in ["true", "active"]
                if not is_active:
                    continue

                pg_id = pg.get("pg_id", key)
                pg_name = pg.get("property_name", pg.get("name", pg.get("pg_name", "")))
                pg_type = pg.get("pg_type", "PG")
                living_type = pg.get("living_type", "")
                city = pg.get("city", "")
                state = pg.get("state", "")
                area = pg.get("area", "")

                # Filters
                if f_pg_id and pg_id != f_pg_id:
                    continue
                if f_city and city.lower() != f_city.lower():
                    continue
                if f_state and state.lower() != f_state.lower():
                    continue
                if f_living_type and living_type.lower() != f_living_type.lower():
                    continue
                if f_property_type and pg_type.lower() != f_property_type.lower():
                    continue
                if f_search:
                    s_term = f_search.lower()
                    if s_term not in pg_id.lower() and s_term not in pg_name.lower() and s_term not in city.lower() and s_term not in area.lower():
                        continue

                rooms_dict = pg.get("rooms", {})
                available_rooms = []
                total_rooms = len(rooms_dict) if isinstance(rooms_dict, dict) else 0
                total_beds = 0
                occupied_beds = 0
                available_beds_count = 0

                if isinstance(rooms_dict, dict):
                    for room_id, r_info in rooms_dict.items():
                        if not isinstance(r_info, dict):
                            continue

                        room_number = r_info.get("room_number", r_info.get("flat_no", r_info.get("name", room_id)))
                        sharing = r_info.get("sharing", r_info.get("bhk", 1))
                        rent = r_info.get("rent", 0)
                        beds_dict = r_info.get("beds", {})

                        available_beds_in_room = []
                        if isinstance(beds_dict, dict) and beds_dict:
                            for bed_id, b_info in beds_dict.items():
                                if not isinstance(b_info, dict):
                                    continue
                                total_beds += 1
                                is_occupied = b_info.get("is_occupied", False)
                                if is_occupied:
                                    occupied_beds += 1
                                else:
                                    available_beds_count += 1
                                    available_beds_in_room.append({
                                        "bed_id": bed_id,
                                        "bed_name": b_info.get("bed_name", b_info.get("name", bed_id))
                                    })

                            if available_beds_in_room:
                                available_rooms.append({
                                    "room_id": room_id,
                                    "room_number": room_number,
                                    "sharing": sharing,
                                    "rent": rent,
                                    "available_beds_count": len(available_beds_in_room),
                                    "available_beds": available_beds_in_room
                                })
                        else:
                            # Apartment / flat unit
                            is_occupied = r_info.get("is_occupied", False)
                            total_beds += 1
                            if is_occupied:
                                occupied_beds += 1
                            else:
                                available_beds_count += 1
                                available_rooms.append({
                                    "room_id": room_id,
                                    "flat_no": room_number,
                                    "bhk": sharing,
                                    "rent": rent,
                                    "is_available": True
                                })

                availability_list.append({
                    "pg_id": pg_id,
                    "pg_name": pg_name,
                    "pg_type": pg_type,
                    "living_type": living_type,
                    "contact_person": pg.get("contact_person", ""),
                    "mobile": pg.get("mobile", ""),
                    "address_line_1": pg.get("address_line_1", ""),
                    "area": area,
                    "city": city,
                    "state": state,
                    "pincode": pg.get("pincode", ""),
                    "total_rooms": total_rooms,
                    "total_beds": total_beds,
                    "occupied_beds": occupied_beds,
                    "available_beds": available_beds_count,
                    "images": pg.get("images", [])
                })

            return Response({
                "message": "Available PGs fetched successfully",
                "total_records": len(availability_list),
                "data": availability_list
            }, status=status.HTTP_200_OK)

        except requests.exceptions.RequestException as e:
            return Response(
                {"detail": f"Failed to fetch PG availability: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class VerifyCheckPaymentView(APIView):
    """
    POST API to verify payment by check.
    Updates the rent_records node for the member to set status = 'paid'.
    """
    def post(self, request):
        member_id = request.data.get("member_id")
        check_number = request.data.get("check_number", "")
        amount = request.data.get("amount")
        
        if not member_id:
            return Response({"detail": "member_id is required."}, status=status.HTTP_400_BAD_REQUEST)
            
        if not DATABASE_URL:
            return Response(
                {"detail": "Firebase database URL is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
            
        rent_record_url = f"{DATABASE_URL}/rent_records/{member_id}.json"
        
        try:
            # 1. Fetch existing rent record
            rent_res = http_session.get(rent_record_url)
            rent_data = rent_res.json()
            
            if not rent_data:
                 return Response({"detail": "Rent record not found for this member."}, status=status.HTTP_404_NOT_FOUND)
                 
            # 2. Update rent status
            patch_data = {
                "status": "paid",
                "payment_method": "check",
                "updated_at": get_ist_now()
            }
            if check_number:
                patch_data["check_number"] = str(check_number)
            if amount is not None:
                patch_data["amount_paid"] = amount
                
            update_res = http_session.patch(rent_record_url, json=patch_data)
            update_res.raise_for_status()
            
            # 3. Add to payments node for dashboard history
            try:
                payment_id = f"PAY_{int(datetime.now().timestamp())}_{member_id}"
                payment_url = f"{DATABASE_URL}/payments/{payment_id}.json"
                payment_data = {
                    "payment_id": payment_id,
                    "member_id": member_id,
                    "pg_id": rent_data.get("pg_id", ""),
                    "amount": amount if amount is not None else rent_data.get("monthly_rent", 0),
                    "payment_method": "check",
                    "check_number": str(check_number),
                    "status": "paid",
                    "created_at": get_ist_now()
                }
                http_session.put(payment_url, json=payment_data)
            except Exception as e:
                pass # Non-critical if it fails to log history
                
            return Response({
                "message": "Payment verified by check successfully.", 
                "member_id": member_id,
                "status": "paid"
            }, status=status.HTTP_200_OK)
            
        except requests.exceptions.RequestException as e:
            return Response(
                {"detail": f"Failed to verify payment: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
