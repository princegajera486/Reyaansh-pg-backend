from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from datetime import timedelta, datetime, date
from zoneinfo import ZoneInfo
import os
import requests
from requests.adapters import HTTPAdapter
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from django.core.cache import cache

from .security import hash_password, create_token, JWTAuthentication, verify_token
from .firebase_client import get_admin_user, create_admin_user, update_last_login, update_admin_profile

load_dotenv()
DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL")

# Reusable HTTP Session with connection pooling for high performance
http_session = requests.Session()
adapter = HTTPAdapter(pool_connections=30, pool_maxsize=30)
http_session.mount("https://", adapter)
http_session.mount("http://", adapter)

def fetch_nodes_parallel(url_dict):
    """
    Fetches multiple Firebase URLs concurrently using thread pool for ultra-fast API responses.
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

class SetupAdminView(APIView):
    """
    Utility endpoint to set up an admin user in Firebase with SHA-256 password.
    """
    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")
        
        if not username or not password:
            return Response({"detail": "Username and password are required."}, status=status.HTTP_400_BAD_REQUEST)

        # Check if user already exists
        try:
            existing_user = get_admin_user(username)
            if existing_user:
                return Response({"detail": "Admin with this username/email already exists."}, status=status.HTTP_409_CONFLICT)
        except Exception as e:
            return Response({"detail": f"Database check failed: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        hashed_pwd = hash_password(password)
        try:
            create_admin_user(username, hashed_pwd)
            return Response({"message": "Admin user created successfully", "username": username}, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class LoginView(APIView):
    """
    Login endpoint. Checks Firebase 'admin' node for the user.
    """
    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")
        
        if not username or not password:
            return Response({"detail": "Username and password are required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user_data = get_admin_user(username)
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        if not user_data:
            return Response({"detail": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)
            
        if not user_data.get("is_active", True):
            return Response({"detail": "Account is disabled"}, status=status.HTTP_403_FORBIDDEN)
        
        # Hash provided password
        hashed_pwd = hash_password(password)
        
        # Compare passwords
        if user_data.get("password") != hashed_pwd:
             return Response({"detail": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)
             
        # Update last_login
        try:
            update_last_login(username)
        except Exception as e:
            # We can just log this or ignore it, shouldn't block login
            print(f"Failed to update last login: {e}")
             
        # Mint tokens
        access_token = create_token(
            data={"sub": username, "type": "access"}, 
            expires_delta=timedelta(days=7)
        )
        refresh_token = create_token(
            data={"sub": username, "type": "refresh"}, 
            expires_delta=timedelta(days=15)
        )
        
        response = Response({
            "message": "Login successful",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer"
        })
        
        # Store in HTTPOnly cookies
        # secure=True should be used in production (requires HTTPS)
        response.set_cookie(
            key="access_token",
            value=access_token,
            httponly=True,
            max_age=7 * 24 * 60 * 60,  # 7 days in seconds
            samesite='Lax'
        )
        response.set_cookie(
            key="refresh_token",
            value=refresh_token,
            httponly=True,
            max_age=15 * 24 * 60 * 60, # 15 days in seconds
            samesite='Lax'
        )
        return response

class LogoutView(APIView):
    """
    Logout endpoint. Clears the access and refresh token cookies.
    """
    def post(self, request):
        response = Response({
            "message": "Logout successful"
        }, status=status.HTTP_200_OK)
        
        response.delete_cookie('access_token')
        response.delete_cookie('refresh_token')
        
        return response

class RefreshTokenView(APIView):
    """
    Refresh endpoint to mint a new access token using a valid refresh token cookie.
    """
    def post(self, request):
        # Look for refresh token in cookies
        refresh_token = request.COOKIES.get('refresh_token')
        
        if not refresh_token:
            return Response({"detail": "Refresh token missing"}, status=status.HTTP_401_UNAUTHORIZED)
            
        payload, error = verify_token(refresh_token)
        if error == "expired":
            return Response({"detail": "Refresh token expired", "code": "token_expired"}, status=status.HTTP_401_UNAUTHORIZED)
        if error == "invalid" or not payload or payload.get("type") != "refresh":
            return Response({"detail": "Invalid refresh token", "code": "token_not_valid"}, status=status.HTTP_401_UNAUTHORIZED)
            
        username = payload.get("sub")
        
        # Mint new access token
        access_token = create_token(
            data={"sub": username, "type": "access"}, 
            expires_delta=timedelta(days=7)
        )
        
        response = Response({
            "message": "Token refreshed successfully",
            "access_token": access_token,
            "token_type": "bearer"
        })
        
        # Set new access token cookie
        response.set_cookie(
            key="access_token",
            value=access_token,
            httponly=True,
            max_age=7 * 24 * 60 * 60,
            samesite='Lax'
        )
        return response

class ProtectedDataView(APIView):
    """
    An example protected POST API. Requires JWT token in header or cookie.
    """
    authentication_classes = [JWTAuthentication]

    def post(self, request):
        if not isinstance(request.user, dict):
            return Response({"detail": "User not authenticated"}, status=status.HTTP_401_UNAUTHORIZED)
            
        username = request.user.get("sub")
        data = request.data.get("data")
        
        return Response({
            "message": f"Hello {username}, this is a protected API.",
            "received_data": data
        })

class AdminDetailsView(APIView):
    """
    GET API to retrieve the currently logged in admin's details.
    Requires a valid JWT token.
    """
    authentication_classes = [JWTAuthentication]

    def get(self, request):
        if not isinstance(request.user, dict):
            return Response({"detail": "User not authenticated"}, status=status.HTTP_401_UNAUTHORIZED)
            
        email = request.user.get("sub")
        if not email:
            return Response({"detail": "User not authenticated"}, status=status.HTTP_401_UNAUTHORIZED)
            
        try:
            admin_data = get_admin_user(email)
            if not admin_data:
                return Response({"detail": "Admin not found"}, status=status.HTTP_404_NOT_FOUND)
                
            # Remove the password from the response for security
            if "password" in admin_data:
                del admin_data["password"]
                
            # Ensure first_name and phone_number are present
            admin_data.setdefault("name", "")
            admin_data.setdefault("phone_number", "")
                
            return Response(admin_data)
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class AdminProfileView(APIView):
    """
    API to retrieve and update the currently logged in admin's profile data.
    Requires a valid JWT token.
    """
    authentication_classes = [JWTAuthentication]

    def get(self, request):
        if not isinstance(request.user, dict):
            return Response({"detail": "User not authenticated"}, status=status.HTTP_401_UNAUTHORIZED)
            
        email = request.user.get("sub")
        if not email:
            return Response({"detail": "User not authenticated"}, status=status.HTTP_401_UNAUTHORIZED)
            
        try:
            admin_data = get_admin_user(email)
            if not admin_data:
                return Response({"detail": "Admin not found"}, status=status.HTTP_404_NOT_FOUND)
                
            # Remove the password from the response for security
            if "password" in admin_data:
                del admin_data["password"]
                
            # Ensure first_name and phone_number are present
            admin_data.setdefault("name", "")
            admin_data.setdefault("phone_number", "")
                
            return Response(admin_data)
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def put(self, request):
        if not isinstance(request.user, dict):
            return Response({"detail": "User not authenticated"}, status=status.HTTP_401_UNAUTHORIZED)
            
        email = request.user.get("sub")
        if not email:
            return Response({"detail": "User not authenticated"}, status=status.HTTP_401_UNAUTHORIZED)
            
        print("Received profile update data:", request.data)
        
        # Accept any fields sent by the frontend, excluding sensitive ones
        restricted_fields = ["admin_id", "password", "last_login", "is_active"]
        profile_data = {
            key: value for key, value in request.data.items() 
            if key not in restricted_fields and value is not None
        }
        
        if not profile_data:
            return Response({"detail": f"No valid profile data provided for update. Received keys: {list(request.data.keys())}"}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            update_admin_profile(email, profile_data)
            
            # Fetch the fully updated profile to return
            updated_admin_data = get_admin_user(email)
            if updated_admin_data and "password" in updated_admin_data:
                del updated_admin_data["password"]
                
            # Ensure first_name and phone_number are present
            if updated_admin_data:
                updated_admin_data.setdefault("name", "")
                updated_admin_data.setdefault("phone_number", "")
            
            return Response({"message": "Profile updated successfully", "data": updated_admin_data or profile_data}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

def parse_date(date_str):
    if not date_str:
        return None
    s = str(date_str).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:19], fmt[:len(s[:19])]).date()
        except Exception:
            pass
    try:
        return datetime.fromisoformat(s).date()
    except Exception:
        pass
    return None

def extract_dashboard_filters(request):
    f_property_type = request.query_params.get("property_type") or request.query_params.get("pg_type") or request.headers.get("property_type")
    f_living_type = request.query_params.get("living_type") or request.headers.get("living_type")
    f_member_status = request.query_params.get("member_status") or request.query_params.get("status") or request.headers.get("member_status")
    f_rent_status = request.query_params.get("rent_status") or request.headers.get("rent_status")
    f_month = request.query_params.get("month") or request.headers.get("month")
    f_year = request.query_params.get("year") or request.headers.get("year")
    f_date_range = request.query_params.get("date_range") or request.headers.get("date_range")
    f_start_date = request.query_params.get("start_date") or request.headers.get("start_date")
    f_end_date = request.query_params.get("end_date") or request.headers.get("end_date")
    f_quick_range = request.query_params.get("quick_range") or request.headers.get("quick_range")

    start_d = None
    end_d = None
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()

    if f_quick_range:
        qr = str(f_quick_range).lower().replace("-", "_").replace(" ", "_")
        if qr in ["today"]:
            start_d = end_d = today
        elif qr in ["yesterday"]:
            start_d = end_d = today - timedelta(days=1)
        elif qr in ["this_week"]:
            start_d = today - timedelta(days=today.weekday())
            end_d = start_d + timedelta(days=6)
        elif qr in ["last_week"]:
            end_d = today - timedelta(days=today.weekday() + 1)
            start_d = end_d - timedelta(days=6)
        elif qr in ["this_month"]:
            start_d = date(today.year, today.month, 1)
            next_m = today.month % 12 + 1
            next_y = today.year + (1 if today.month == 12 else 0)
            end_d = date(next_y, next_m, 1) - timedelta(days=1)
        elif qr in ["last_month"]:
            last_m = today.month - 1 if today.month > 1 else 12
            last_y = today.year - (1 if today.month == 1 else 0)
            start_d = date(last_y, last_m, 1)
            end_d = date(today.year, today.month, 1) - timedelta(days=1)
        elif qr in ["this_year"]:
            start_d = date(today.year, 1, 1)
            end_d = date(today.year, 12, 31)
        elif qr in ["7days", "last_7_days", "7_days"]:
            start_d = today - timedelta(days=7)
            end_d = today
        elif qr in ["30days", "last_30_days", "30_days"]:
            start_d = today - timedelta(days=30)
            end_d = today
        elif qr in ["90days", "last_90_days", "90_days"]:
            start_d = today - timedelta(days=90)
            end_d = today

    if not start_d and not end_d and f_date_range:
        parts = [p.strip() for p in f_date_range.replace("to", ",").split(",") if p.strip()]
        if len(parts) >= 2:
            start_d = parse_date(parts[0])
            end_d = parse_date(parts[1])
        elif len(parts) == 1:
            start_d = parse_date(parts[0])

    if f_start_date and not start_d:
        start_d = parse_date(f_start_date)
    if f_end_date and not end_d:
        end_d = parse_date(f_end_date)

    month_num = None
    if f_month:
        m_str = str(f_month).strip()
        if m_str.isdigit():
            month_num = int(m_str)
        else:
            for i in range(1, 13):
                m_name = date(2000, i, 1).strftime("%B").lower()
                m_abbr = date(2000, i, 1).strftime("%b").lower()
                if m_str.lower() in (m_name, m_abbr):
                    month_num = i
                    break

    year_num = int(f_year) if f_year and str(f_year).isdigit() else None

    return {
        "property_type": f_property_type,
        "living_type": f_living_type,
        "member_status": f_member_status,
        "rent_status": f_rent_status,
        "month": month_num,
        "year": year_num,
        "start_date": start_d,
        "end_date": end_d
    }

def match_date_filter(dt_val, filters):
    if not dt_val:
        return True
    parsed_dt = parse_date(dt_val)
    if not parsed_dt:
        return True
    if filters["start_date"] and parsed_dt < filters["start_date"]:
        return False
    if filters["end_date"] and parsed_dt > filters["end_date"]:
        return False
    if filters["month"] and parsed_dt.month != filters["month"]:
        return False
    if filters["year"] and parsed_dt.year != filters["year"]:
        return False
    return True

class DashboardKPIView(APIView):
    """
    GET API to calculate and return dashboard KPI metrics dynamically from Firebase.
    """
    authentication_classes = [JWTAuthentication]

    def get(self, request):
        if not DATABASE_URL:
            return Response(
                {"detail": "Firebase database URL is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        filters = extract_dashboard_filters(request)

        try:
            nodes = fetch_nodes_parallel({
                "pgs": f"{DATABASE_URL}/pg_properties.json",
                "members": f"{DATABASE_URL}/members.json",
                "rent": f"{DATABASE_URL}/rent_records.json"
            })
            pgs_data = nodes["pgs"]
            members_data = nodes["members"]
            rent_data = nodes["rent"]

            total_pgs = 0
            active_pgs = 0
            inactive_pgs = 0
            total_rooms = 0
            total_beds = 0
            occupied_beds = 0

            for pg_id, pg_info in pgs_data.items():
                if not isinstance(pg_info, dict):
                    continue
                pg_type = pg_info.get("pg_type", "")
                living_type = pg_info.get("living_type", "")
                if filters["property_type"] and pg_type.lower() != filters["property_type"].lower():
                    continue
                if filters["living_type"] and living_type.lower() != filters["living_type"].lower():
                    continue

                total_pgs += 1
                status_val = pg_info.get("property_status")
                if status_val is True or str(status_val).lower() in ["true", "active"]:
                    active_pgs += 1
                else:
                    inactive_pgs += 1

                rooms = pg_info.get("rooms", {})
                if isinstance(rooms, dict):
                    total_rooms += len(rooms)
                    for r_id, r_info in rooms.items():
                        if isinstance(r_info, dict):
                            beds = r_info.get("beds", {})
                            if isinstance(beds, dict):
                                total_beds += len(beds)
                                for b_id, b_info in beds.items():
                                    if isinstance(b_info, dict) and b_info.get("is_occupied"):
                                        mem_id = b_info.get("member_id")
                                        if mem_id and mem_id in members_data:
                                            mem = members_data[mem_id]
                                            if not mem.get("is_deleted") and mem.get("status") not in ["Inactive", "Deleted"]:
                                                occupied_beds += 1
                                        elif not mem_id:
                                            occupied_beds += 1

            total_members = 0
            active_members = 0
            notice_members = 0

            for m_id, m_info in members_data.items():
                if not isinstance(m_info, dict) or m_info.get("is_deleted"):
                    continue

                pg_id = m_info.get("pg_id", "")
                pg_info = pgs_data.get(pg_id, {})
                if filters["property_type"] and pg_info.get("pg_type", "").lower() != filters["property_type"].lower():
                    continue
                if filters["living_type"] and pg_info.get("living_type", "").lower() != filters["living_type"].lower():
                    continue

                m_status = m_info.get("status", "")
                if filters["member_status"] and m_status.lower() != filters["member_status"].lower():
                    continue

                m_dt = m_info.get("created_at") or m_info.get("joining_date")
                # Removed date filter for snapshot metric (Total Members) so it matches absolute occupied beds

                total_members += 1
                if m_status == "Active":
                    active_members += 1
                elif m_status == "Notice Period":
                    notice_members += 1

            rent_collected_amount = 0
            pending_rent_amount = 0
            pending_members_count = 0

            for r_id, r_info in rent_data.items():
                if not isinstance(r_info, dict):
                    continue

                r_status = str(r_info.get("status", "")).lower()
                if filters["rent_status"] and r_status != filters["rent_status"].lower():
                    continue

                m_id = r_info.get("member_id", r_id)
                m_info = members_data.get(m_id, {})
                if filters["member_status"] and m_info.get("status", "").lower() != filters["member_status"].lower():
                    continue

                pg_id = r_info.get("pg_id") or m_info.get("pg_id", "")
                pg_info = pgs_data.get(pg_id, {})
                if filters["property_type"] and pg_info.get("pg_type", "").lower() != filters["property_type"].lower():
                    continue
                if filters["living_type"] and pg_info.get("living_type", "").lower() != filters["living_type"].lower():
                    continue

                r_dt = r_info.get("updated_at") or r_info.get("created_at") or r_info.get("rent_due_date")
                if not match_date_filter(r_dt, filters):
                    continue

                try:
                    monthly_rent = int(r_info.get("monthly_rent", 0))
                except (ValueError, TypeError):
                    monthly_rent = 0

                if r_status == "paid":
                    rent_collected_amount += monthly_rent
                else:
                    pending_rent_amount += monthly_rent
                    pending_members_count += 1

            if total_beds > 0:
                occupancy_perc = round((occupied_beds / total_beds) * 100)
            elif total_rooms > 0:
                occupancy_perc = round((active_members / total_rooms) * 100)
            else:
                occupancy_perc = 0

            kpis = {
                "total_pgs": {
                    "value": total_pgs,
                    "active": active_pgs,
                    "inactive": inactive_pgs
                },
                "total_rooms": total_rooms,
                "total_members": {
                    "value": total_members,
                    "active": active_members,
                    "notice": notice_members
                },
                "occupancy_rate": {
                    "percentage": occupancy_perc,
                    "occupied": occupied_beds if total_beds > 0 else active_members,
                    "trend": "+2.5%"
                },
                "rent_collected": {
                    "amount": rent_collected_amount,
                    "trend": "+12.4%"
                },
                "pending_rent": {
                    "amount": pending_rent_amount,
                    "members": pending_members_count,
                    "trend": "+8.7%"
                }
            }

            return Response({"kpis": kpis}, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class DashboardChartsView(APIView):
    """
    GET API to calculate and return dashboard charts data dynamically from Firebase.
    """
    authentication_classes = [JWTAuthentication]

    def get(self, request):
        if not DATABASE_URL:
            return Response(
                {"detail": "Firebase database URL is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        filters = extract_dashboard_filters(request)

        try:
            nodes = fetch_nodes_parallel({
                "pgs": f"{DATABASE_URL}/pg_properties.json",
                "members": f"{DATABASE_URL}/members.json"
            })
            pgs_data = nodes["pgs"]
            members_data = nodes["members"]

            total_beds = 0
            occupied_beds = 0
            pg_revenue_map = {}

            for pg_id, pg_info in pgs_data.items():
                if not isinstance(pg_info, dict):
                    continue
                pg_type = pg_info.get("pg_type", "")
                living_type = pg_info.get("living_type", "")
                if filters["property_type"] and pg_type.lower() != filters["property_type"].lower():
                    continue
                if filters["living_type"] and living_type.lower() != filters["living_type"].lower():
                    continue

                pg_name = pg_info.get("property_name", pg_info.get("pg_name", pg_info.get("name", pg_id)))
                pg_revenue_map[pg_name] = 0

                rooms = pg_info.get("rooms", {})
                if isinstance(rooms, dict):
                    for r_id, r_info in rooms.items():
                        if isinstance(r_info, dict):
                            beds = r_info.get("beds", {})
                            if isinstance(beds, dict):
                                total_beds += len(beds)
                                for b_id, b_info in beds.items():
                                    if isinstance(b_info, dict) and b_info.get("is_occupied"):
                                        mem_id = b_info.get("member_id")
                                        if mem_id and mem_id in members_data:
                                            mem = members_data[mem_id]
                                            if not mem.get("is_deleted") and mem.get("status") not in ["Inactive", "Deleted"]:
                                                occupied_beds += 1
                                        elif not mem_id:
                                            occupied_beds += 1

            vacant_beds = max(0, total_beds - occupied_beds)

            occ_total = total_beds if total_beds > 0 else (occupied_beds + vacant_beds)
            if occ_total > 0:
                occ_perc = round((occupied_beds / occ_total) * 100, 1)
                vac_perc = round((vacant_beds / occ_total) * 100, 1)
            else:
                occ_perc, vac_perc = 0.0, 0.0

            occupancy_overview = {
                "occupied_beds": {"count": occupied_beds, "percentage": occ_perc},
                "vacant_beds": {"count": vacant_beds, "percentage": vac_perc}
            }

            active_cnt = 0
            notice_cnt = 0
            inactive_cnt = 0
            vacant_left_cnt = 0

            monthly_trend_map = {}

            for m_id, m_info in members_data.items():
                if not isinstance(m_info, dict):
                    continue

                pg_id = m_info.get("pg_id", "")
                pg_info = pgs_data.get(pg_id, {})
                if filters["property_type"] and pg_info.get("pg_type", "").lower() != filters["property_type"].lower():
                    continue
                if filters["living_type"] and pg_info.get("living_type", "").lower() != filters["living_type"].lower():
                    continue

                m_status = m_info.get("status", "")
                if filters["member_status"] and m_status.lower() != filters["member_status"].lower():
                    continue

                created_at = m_info.get("created_at") or m_info.get("joining_date", "")
                # Removed date filter for snapshot metrics so member distribution is accurate

                is_deleted = m_info.get("is_deleted")
                rent_val = 0
                try:
                    rent_val = int(m_info.get("monthly_rent", 0))
                except (ValueError, TypeError):
                    pass

                pg_name = pg_info.get("property_name", pg_info.get("pg_name", pg_info.get("name", pg_id)))
                if pg_name in pg_revenue_map:
                    pg_revenue_map[pg_name] += rent_val

                if created_at:
                    try:
                        dt = datetime.fromisoformat(created_at)
                        month_abbr = dt.strftime("%b")
                        monthly_trend_map[month_abbr] = monthly_trend_map.get(month_abbr, 0) + rent_val
                    except Exception:
                        pass

                if is_deleted:
                    vacant_left_cnt += 1
                elif m_status == "Active":
                    active_cnt += 1
                elif m_status == "Notice Period":
                    notice_cnt += 1
                elif m_status == "Inactive":
                    inactive_cnt += 1
                else:
                    active_cnt += 1

            total_dist_base = active_cnt + notice_cnt + inactive_cnt + vacant_left_cnt
            if total_dist_base == 0:
                total_dist_base = 1

            member_status_distribution = {
                "active": {
                    "count": active_cnt,
                    "percentage": round((active_cnt / total_dist_base) * 100, 1)
                },
                "notice_period": {
                    "count": notice_cnt,
                    "percentage": round((notice_cnt / total_dist_base) * 100, 1)
                },
                "inactive": {
                    "count": inactive_cnt,
                    "percentage": round((inactive_cnt / total_dist_base) * 100, 1)
                },
                "vacant_left": {
                    "count": vacant_left_cnt,
                    "percentage": round((vacant_left_cnt / total_dist_base) * 100, 1)
                }
            }

            revenue_by_pg = [
                {"pg_name": name, "revenue": rev}
                for name, rev in pg_revenue_map.items()
            ]
            revenue_by_pg.sort(key=lambda x: x["revenue"], reverse=True)

            monthly_rent_collection_trend = []
            for month_name, amt in monthly_trend_map.items():
                monthly_rent_collection_trend.append({"month": month_name, "amount": amt})

            return Response({
                "monthly_rent_collection_trend": monthly_rent_collection_trend,
                "revenue_by_pg": revenue_by_pg,
                "occupancy_overview": occupancy_overview,
                "member_status_distribution": member_status_distribution
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class DashboardTablesView(APIView):
    """
    GET API to calculate and return dashboard tables data (upcoming rent dues and recent payments) dynamically from Firebase.
    """
    authentication_classes = [JWTAuthentication]

    def get(self, request):
        if not DATABASE_URL:
            return Response(
                {"detail": "Firebase database URL is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        filters = extract_dashboard_filters(request)

        try:
            nodes = fetch_nodes_parallel({
                "pgs": f"{DATABASE_URL}/pg_properties.json",
                "members": f"{DATABASE_URL}/members.json",
                "rent": f"{DATABASE_URL}/rent_records.json",
                "payments": f"{DATABASE_URL}/payments.json"
            })
            pgs_data = nodes["pgs"]
            members_data = nodes["members"]
            rent_data = nodes["rent"]
            payments_data = nodes["payments"]

            today = datetime.now(ZoneInfo("Asia/Kolkata")).date()

            upcoming_rent_due = []
            recent_payments = []

            member_map = {m_id: m_info for m_id, m_info in members_data.items() if isinstance(m_info, dict)}
            pg_map = {pg_id: pg_info for pg_id, pg_info in pgs_data.items() if isinstance(pg_info, dict)}

            for r_id, r_info in rent_data.items():
                if not isinstance(r_info, dict):
                    continue

                r_status = str(r_info.get("status", "")).lower()
                if filters["rent_status"] and r_status != filters["rent_status"].lower():
                    continue

                m_id = r_info.get("member_id", r_id)
                m_info = member_map.get(m_id, {})
                if m_info.get("is_deleted"):
                    continue

                if filters["member_status"] and m_info.get("status", "").lower() != filters["member_status"].lower():
                    continue

                pg_id = r_info.get("pg_id", m_info.get("pg_id", ""))
                pg_info = pg_map.get(pg_id, {})
                if filters["property_type"] and pg_info.get("pg_type", "").lower() != filters["property_type"].lower():
                    continue
                if filters["living_type"] and pg_info.get("living_type", "").lower() != filters["living_type"].lower():
                    continue

                r_dt = r_info.get("updated_at") or r_info.get("created_at") or r_info.get("rent_due_date")
                if not match_date_filter(r_dt, filters):
                    continue

                m_name = m_info.get("full_name", r_info.get("member_name", "Unknown Member"))
                pg_name = pg_info.get("property_name", pg_info.get("pg_name", pg_info.get("name", pg_id)))

                room_id = m_info.get("room_id", "")
                room_number = room_id
                if pg_id and "rooms" in pg_info and isinstance(pg_info.get("rooms"), dict) and room_id in pg_info["rooms"]:
                    room_info = pg_info["rooms"][room_id]
                    if isinstance(room_info, dict):
                        room_number = room_info.get("room_number", room_info.get("room_name", room_number))

                try:
                    rent_amt = int(r_info.get("monthly_rent", m_info.get("monthly_rent", 0)))
                except (ValueError, TypeError):
                    rent_amt = 0

                due_date_str = str(r_info.get("rent_due_date", m_info.get("rent_due_date", "")))

                days_left = 0
                formatted_due_date = due_date_str
                if due_date_str:
                    try:
                        if len(due_date_str) <= 2:
                            day_num = int(due_date_str)
                            due_dt = date(today.year, today.month, day_num)
                            if due_dt < today:
                                month_val = today.month % 12 + 1
                                year_val = today.year + (1 if today.month == 12 else 0)
                                due_dt = date(year_val, month_val, day_num)
                            formatted_due_date = due_dt.strftime("%Y-%m-%d")
                            days_left = (due_dt - today).days
                        else:
                            dt = datetime.fromisoformat(due_date_str).date()
                            formatted_due_date = dt.strftime("%Y-%m-%d")
                            days_left = (dt - today).days
                    except Exception:
                        pass

                if r_status == "paid":
                    recent_payments.append({
                        "payment_id": f"PAY_{r_id}",
                        "member_id": m_id,
                        "pg_id": pg_id,
                        "member_name": m_name,
                        "pg_name": pg_name,
                        "amount": rent_amt,
                        "date": r_info.get("updated_at", r_info.get("created_at", datetime.now().isoformat())),
                        "status": "Paid"
                    })
                else:
                    upcoming_rent_due.append({
                        "rent_id": r_id,
                        "member_id": m_id,
                        "pg_id": pg_id,
                        "member_name": m_name,
                        "pg_name": pg_name,
                        "room_number": room_number,
                        "rent_amount": rent_amt,
                        "due_date": formatted_due_date,
                        "days_left": max(0, days_left),
                        "status": "Upcoming" if days_left >= 0 else "Overdue"
                    })

            if payments_data and isinstance(payments_data, dict):
                recent_payments = []
                for p_id, p_info in payments_data.items():
                    if not isinstance(p_info, dict):
                        continue
                    m_id = p_info.get("member_id", "")
                    m_info = member_map.get(m_id, {})
                    if filters["member_status"] and m_info.get("status", "").lower() != filters["member_status"].lower():
                        continue
                    pg_id = p_info.get("pg_id", m_info.get("pg_id", ""))
                    pg_info = pg_map.get(pg_id, {})
                    if filters["property_type"] and pg_info.get("pg_type", "").lower() != filters["property_type"].lower():
                        continue
                    if filters["living_type"] and pg_info.get("living_type", "").lower() != filters["living_type"].lower():
                        continue
                    p_status = p_info.get("status", "Paid")
                    if filters["rent_status"] and str(p_status).lower() != filters["rent_status"].lower():
                        continue
                    p_dt = p_info.get("date") or p_info.get("created_at")
                    if not match_date_filter(p_dt, filters):
                        continue

                    pg_name = pg_info.get("property_name", pg_info.get("pg_name", pg_info.get("name", pg_id)))

                    try:
                        amt = int(p_info.get("amount", p_info.get("monthly_rent", 0)))
                    except (ValueError, TypeError):
                        amt = 0

                    recent_payments.append({
                        "payment_id": p_id,
                        "member_id": m_id,
                        "pg_id": pg_id,
                        "member_name": m_info.get("full_name", p_info.get("member_name", "Unknown Member")),
                        "pg_name": pg_name,
                        "amount": amt,
                        "date": p_info.get("date", p_info.get("created_at", datetime.now().isoformat())),
                        "status": p_status
                    })

            return Response({
                "upcoming_rent_due": upcoming_rent_due,
                "recent_payments": recent_payments
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class DashboardAlertsView(APIView):
    """
    GET API to calculate and return dashboard alerts (rent overdue and pending payment approvals) dynamically from Firebase.
    """
    authentication_classes = [JWTAuthentication]

    def get(self, request):
        if not DATABASE_URL:
            return Response(
                {"detail": "Firebase database URL is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        filters = extract_dashboard_filters(request)

        try:
            nodes = fetch_nodes_parallel({
                "pgs": f"{DATABASE_URL}/pg_properties.json",
                "members": f"{DATABASE_URL}/members.json",
                "rent": f"{DATABASE_URL}/rent_records.json",
                "payments": f"{DATABASE_URL}/payments.json",
                "pending_approvals": f"{DATABASE_URL}/pending_approvals.json"
            })
            pgs_data = nodes["pgs"]
            members_data = nodes["members"]
            rent_data = nodes["rent"]
            payments_data = nodes["payments"]
            pending_appr_data = nodes["pending_approvals"]
            today = datetime.now(ZoneInfo("Asia/Kolkata")).date()

            rent_overdue = []
            pending_approvals = []

            member_map = {m_id: m_info for m_id, m_info in members_data.items() if isinstance(m_info, dict)}
            pg_map = {pg_id: pg_info for pg_id, pg_info in pgs_data.items() if isinstance(pg_info, dict)}

            for r_id, r_info in rent_data.items():
                if not isinstance(r_info, dict):
                    continue

                r_status = str(r_info.get("status", "")).lower()
                if r_status == "paid":
                    continue
                if filters["rent_status"] and r_status != filters["rent_status"].lower():
                    continue

                m_id = r_info.get("member_id", r_id)
                m_info = member_map.get(m_id, {})
                if m_info.get("is_deleted"):
                    continue
                if filters["member_status"] and m_info.get("status", "").lower() != filters["member_status"].lower():
                    continue

                pg_id = r_info.get("pg_id", m_info.get("pg_id", ""))
                pg_info = pg_map.get(pg_id, {})
                if filters["property_type"] and pg_info.get("pg_type", "").lower() != filters["property_type"].lower():
                    continue
                if filters["living_type"] and pg_info.get("living_type", "").lower() != filters["living_type"].lower():
                    continue

                r_dt = r_info.get("updated_at") or r_info.get("created_at") or r_info.get("rent_due_date")
                if not match_date_filter(r_dt, filters):
                    continue

                m_name = m_info.get("full_name", r_info.get("member_name", "Unknown Member"))
                pg_name = pg_info.get("property_name", pg_info.get("pg_name", pg_info.get("name", pg_id)))

                room_id = m_info.get("room_id", "")
                room_number = room_id
                if pg_id and "rooms" in pg_info and isinstance(pg_info.get("rooms"), dict) and room_id in pg_info["rooms"]:
                    room_info = pg_info["rooms"][room_id]
                    if isinstance(room_info, dict):
                        room_number = room_info.get("room_number", room_info.get("room_name", room_number))

                try:
                    rent_amt = int(r_info.get("monthly_rent", m_info.get("monthly_rent", 0)))
                except (ValueError, TypeError):
                    rent_amt = 0

                due_date_str = str(r_info.get("rent_due_date", m_info.get("rent_due_date", "")))

                overdue_by_days = 0
                formatted_due_date = due_date_str

                if due_date_str:
                    try:
                        if len(due_date_str) <= 2:
                            day_num = int(due_date_str)
                            due_dt = date(today.year, today.month, day_num)
                            if due_dt > today:
                                month_val = today.month - 1 if today.month > 1 else 12
                                year_val = today.year - (1 if today.month == 1 else 0)
                                due_dt = date(year_val, month_val, day_num)
                            formatted_due_date = due_dt.strftime("%Y-%m-%d")
                            overdue_by_days = (today - due_dt).days
                        else:
                            dt = datetime.fromisoformat(due_date_str).date()
                            formatted_due_date = dt.strftime("%Y-%m-%d")
                            overdue_by_days = (today - dt).days
                    except Exception:
                        pass

                if overdue_by_days > 0 or r_status == "overdue":
                    rent_overdue.append({
                        "rent_id": r_id,
                        "member_id": m_id,
                        "pg_id": pg_id,
                        "member_name": m_name,
                        "pg_name": pg_name,
                        "room_number": room_number,
                        "rent_amount": rent_amt,
                        "due_date": formatted_due_date,
                        "overdue_by_days": max(1, overdue_by_days),
                        "status": "Overdue"
                    })

            combined_pending = {}
            if pending_appr_data and isinstance(pending_appr_data, dict):
                combined_pending.update(pending_appr_data)

            if payments_data and isinstance(payments_data, dict):
                for p_id, p_info in payments_data.items():
                    if isinstance(p_info, dict) and str(p_info.get("status", "")).lower() in ["pending", "pending_approval", "pending approval"]:
                        combined_pending[p_id] = p_info

            for p_id, p_info in combined_pending.items():
                if not isinstance(p_info, dict):
                    continue
                m_id = p_info.get("member_id", "")
                m_info = member_map.get(m_id, {})
                if filters["member_status"] and m_info.get("status", "").lower() != filters["member_status"].lower():
                    continue

                pg_id = p_info.get("pg_id", m_info.get("pg_id", ""))
                pg_info = pg_map.get(pg_id, {})
                if filters["property_type"] and pg_info.get("pg_type", "").lower() != filters["property_type"].lower():
                    continue
                if filters["living_type"] and pg_info.get("living_type", "").lower() != filters["living_type"].lower():
                    continue

                p_dt = p_info.get("submitted_on") or p_info.get("date") or p_info.get("created_at")
                if not match_date_filter(p_dt, filters):
                    continue

                pg_name = pg_info.get("property_name", pg_info.get("pg_name", pg_info.get("name", pg_id)))

                try:
                    amt = int(p_info.get("amount", p_info.get("monthly_rent", 0)))
                except (ValueError, TypeError):
                    amt = 0

                pending_approvals.append({
                    "payment_id": p_id,
                    "member_id": m_id,
                    "pg_id": pg_id,
                    "member_name": m_info.get("full_name", p_info.get("member_name", "Unknown Member")),
                    "pg_name": pg_name,
                    "amount": amt,
                    "payment_type": p_info.get("payment_type", p_info.get("mode", "UPI")),
                    "submitted_on": p_info.get("submitted_on", p_info.get("created_at", datetime.now().isoformat()))
                })

            return Response({
                "rent_overdue": rent_overdue,
                "pending_approvals": pending_approvals
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

from icalendar import Calendar, Event, vCalAddress, vText
import uuid
from django.core.mail import EmailMessage
from django.conf import settings

class SendCalendarReminderView(APIView):
    """
    POST API to send an ICS calendar reminder via email.
    Expected body:
    {
        "email": "user@example.com",
        "title": "Rent Reminder",
        "description": "Please pay your rent for this month.",
        "start_time": "2026-09-01T10:00:00", // Optional
        "end_time": "2026-09-01T11:00:00",   // Optional
        "member_id": "-O123456789" // Optional
    }
    """
    def post(self, request):
        email_arg = request.data.get("email")
        member_id_arg = request.data.get("member_id")
        title_arg = request.data.get("title", "Rent Reminder")
        description_arg = request.data.get("description", "This is a reminder to pay your rent.")
        checkout_url_arg = request.data.get("checkout_url")
        
        start_time_str_arg = request.data.get("start_time")
        end_time_str_arg = request.data.get("end_time")

        if not email_arg and not member_id_arg:
            return Response({"error": "Either email or member_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        import threading
        import os
        
        def process_and_send(email, member_id, title, description, checkout_url, start_time_str, end_time_str):
            rent_due_date_str = None
            is_active = False

            if member_id:
                DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL")
                if DATABASE_URL:
                    try:
                        res = http_session.get(f"{DATABASE_URL}/members/{member_id}.json", timeout=5)
                        if res.status_code == 200 and res.json():
                            member_data = res.json()
                            if not email:
                                email = member_data.get("email", member_data.get("email_id"))
                            rent_due_date_str = str(member_data.get("rent_due_date", ""))
                            status_val = str(member_data.get("status", "")).lower()
                            is_active = (status_val == "active")
                    except Exception as e:
                        print(f"Background error: Failed to fetch member from DB: {str(e)}")
                        return

            if not email:
                print("Background error: An email address is required to send the calendar invite.")
                return

            # Parse dates or use defaults
            recurrence_day = None
            try:
                today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
                if start_time_str:
                    start_time = datetime.fromisoformat(start_time_str)
                elif rent_due_date_str and is_active:
                    # Calculate start_time based on rent_due_date
                    try:
                        if len(rent_due_date_str) <= 2:
                            day_num = int(rent_due_date_str)
                            recurrence_day = day_num
                        else:
                            dt = datetime.fromisoformat(rent_due_date_str)
                            recurrence_day = dt.day
                            
                        # Next occurrence
                        due_dt = date(today.year, today.month, recurrence_day)
                        if due_dt < today:
                            month_val = today.month % 12 + 1
                            year_val = today.year + (1 if today.month == 12 else 0)
                            due_dt = date(year_val, month_val, recurrence_day)
                        
                        # Assume 11:30 PM IST on the due date
                        start_time = datetime(due_dt.year, due_dt.month, due_dt.day, 23, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
                    except ValueError:
                        start_time = datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(days=1)
                else:
                    start_time = datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(days=1)
                    
                if end_time_str:
                    end_time = datetime.fromisoformat(end_time_str)
                else:
                    end_time = start_time + timedelta(hours=1)
            except ValueError as e:
                print(f"Background error: Invalid date format: {e}")
                return

            try:
                # Use the actual sender email to avoid spam flags
                sender_email = settings.EMAIL_HOST_USER

                # Create the calendar event
                cal = Calendar()
                cal.add('prodid', f'-//PgAdmin Reminder System//{sender_email}//')
                cal.add('version', '2.0')
                cal.add('method', 'REQUEST') # Very important for calendar invites to show up properly

                # Append checkout URL to calendar description
                cal_description = description
                if checkout_url:
                    cal_description += f"\n\nPay online here: {checkout_url}"

                event = Event()
                event.add('summary', title)
                event.add('dtstart', start_time)
                event.add('dtend', end_time)
                event.add('dtstamp', datetime.now(ZoneInfo("UTC")))
                event.add('description', cal_description)
                event['uid'] = f"{uuid.uuid4()}@{sender_email.split('@')[-1]}"
                event.add('priority', 5)
                event.add('status', 'CONFIRMED')

                # Add recurrence rule if it's based on a rent due date and member is active
                if recurrence_day and is_active:
                    event.add('rrule', {'freq': 'monthly', 'bymonthday': recurrence_day})

                # Add organizer and attendee using real emails
                organizer = vCalAddress(f'MAILTO:{sender_email}')
                organizer.params['cn'] = vText('PgAdmin')
                event['organizer'] = organizer

                attendee = vCalAddress(f'MAILTO:{email}')
                attendee.params['cn'] = vText('Guest')
                attendee.params['ROLE'] = vText('REQ-PARTICIPANT')
                attendee.params['PARTSTAT'] = vText('NEEDS-ACTION')
                attendee.params['RSVP'] = vText('TRUE')
                event.add('attendee', attendee, encode=0)

                cal.add_component(event)
                ics_content = cal.to_ical()

                # Create a more formal email body with checkout link
                formal_body = f"Hello,\n\n{description}\n\n"
                if checkout_url:
                    formal_body += f"You can securely pay your rent online using the following link:\n{checkout_url}\n\n"
                
                formal_body += (
                    f"We have attached a calendar invitation for your convenience. "
                    f"Please add this event to your calendar.\n\n"
                    f"Best regards,\n"
                    f"PgAdmin Management"
                )

                # Send Email
                email_msg = EmailMessage(
                    subject=title,
                    body=formal_body,
                    from_email=sender_email, 
                    to=[email],
                )
                
                # Attach the .ics file
                email_msg.attach('invite.ics', ics_content, 'text/calendar')
                
                # Send it
                email_msg.send(fail_silently=False)
                print(f"Background success: Calendar invite sent to {email}")

            except Exception as e:
                print(f"Background error: Failed to send calendar invite: {str(e)}")

        # Start the background thread immediately
        threading.Thread(
            target=process_and_send, 
            args=(email_arg, member_id_arg, title_arg, description_arg, checkout_url_arg, start_time_str_arg, end_time_str_arg)
        ).start()

        return Response({"message": "Calendar invite is being processed and sent in the background."}, status=status.HTTP_202_ACCEPTED)

class SendRentReminderEmailView(APIView):
    """
    POST API to send a professional rent reminder email WITHOUT a calendar invite.
    Expected body:
    {
        "member_id": "-O123456789",
        "title": "Rent Payment Due",
        "description": "This is a polite reminder that your rent is due.",
        "checkout_url": "https://pgadmin-your-domain.com/checkout?id=123" // Optional
    }
    """
    def post(self, request):
        member_id_arg = request.data.get("member_id")
        title_arg = request.data.get("title", "Rent Payment Due")
        description_arg = request.data.get("description", "This is a polite reminder that your rent is due.")
        checkout_url_arg = request.data.get("checkout_url")

        if not member_id_arg:
            return Response({"error": "member_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        import threading
        import os

        def process_and_send_email(member_id, title, description, checkout_url):
            DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL")
            if not DATABASE_URL:
                print("Background error: FIREBASE_DATABASE_URL not set.")
                return

            try:
                # 1. Fetch member data
                res = http_session.get(f"{DATABASE_URL}/members/{member_id}.json", timeout=5)
                if res.status_code != 200 or not res.json():
                    print(f"Background error: Member {member_id} not found.")
                    return
                
                member_data = res.json()
                email = member_data.get("email", member_data.get("email_id"))
                name = member_data.get("name", "Member")
                rent_amt = member_data.get("monthly_rent", "0")
                
                if not email:
                    print(f"Background error: No email found for member {member_id}.")
                    return

                # 2. Build a professional email body
                body = f"Dear {name},\n\n"
                body += f"{description}\n\n"
                body += f"Monthly Rent Amount: ₹{rent_amt}\n\n"

                if checkout_url:
                    body += f"To make your payment securely online, please click the link below:\n{checkout_url}\n\n"
                
                body += "If you have already made the payment, please ignore this email.\n\n"
                body += "Best regards,\nPgAdmin Management"

                # 3. Send email
                sender_email = settings.EMAIL_HOST_USER
                email_msg = EmailMessage(
                    subject=title,
                    body=body,
                    from_email=sender_email, 
                    to=[email],
                )
                
                email_msg.send(fail_silently=False)
                print(f"Background success: Reminder email sent to {email}")

            except Exception as e:
                print(f"Background error: Failed to send reminder email: {str(e)}")

        # Start the background thread
        threading.Thread(
            target=process_and_send_email, 
            args=(member_id_arg, title_arg, description_arg, checkout_url_arg)
        ).start()

        return Response({"message": "Rent reminder email is being processed and sent in the background."}, status=status.HTTP_202_ACCEPTED)

class TriggerDailyRemindersView(APIView):
    """
    GET or POST API to automatically sweep the database and send rent reminders to all members 
    whose rent is due today. This endpoint is designed to be hit daily by a cron job.
    """
    def post(self, request):
        return self._process_sweep()
        
    def get(self, request):
        return self._process_sweep()
        
    def _process_sweep(self):
        import threading
        import os

        def run_sweep_in_background():
            DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL")
            if not DATABASE_URL:
                print("Bulk Sweep Error: FIREBASE_DATABASE_URL not set.")
                return

            try:
                # 1. Fetch all members
                res = http_session.get(f"{DATABASE_URL}/members.json", timeout=15)
                if res.status_code != 200 or not res.json():
                    print("Bulk Sweep: No members found in database.")
                    return
                
                members_data = res.json()
                today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
                emails_sent_count = 0
                
                # 2. Iterate through all members
                for member_id, member in members_data.items():
                    if not member:
                        continue
                        
                    # Check active status
                    status_val = str(member.get("status", "")).lower()
                    if status_val != "active":
                        continue
                        
                    email = member.get("email", member.get("email_id"))
                    if not email:
                        continue
                        
                    # 3. Check if rent is due today
                    rent_due_date_str = str(member.get("rent_due_date", ""))
                    if not rent_due_date_str:
                        continue
                        
                    is_due_today = False
                    if len(rent_due_date_str) <= 2:
                        try:
                            day_num = int(rent_due_date_str)
                            if today.day == day_num:
                                is_due_today = True
                        except ValueError:
                            pass
                    else:
                        try:
                            dt = datetime.fromisoformat(rent_due_date_str).date()
                            if dt.day == today.day:
                                is_due_today = True
                        except ValueError:
                            pass
                            
                    if not is_due_today:
                        continue
                        
                    # 4. Extract dynamic data
                    name = member.get("name", "Member")
                    pg_name = member.get("pg_name", "your PG")
                    room_number = member.get("room_number", "your room")
                    rent_amt = member.get("monthly_rent", "0")
                    
                    # 5. Construct Checkout URL
                    checkout_url = f"https://reyaansh-pg.vercel.app/checkout?member_id={member_id}"
                    
                    # 6. Build highly personalized professional email
                    subject = f"Action Required: Rent Payment Due Today - {pg_name}"
                    
                    body = f"Dear {name},\n\n"
                    body += f"This is an automated reminder that your monthly rent for {pg_name} (Room {room_number}) is due today.\n\n"
                    body += f"Monthly Rent Amount: ₹{rent_amt}\n\n"
                    body += f"To make your payment securely online, please click your personal checkout link below:\n{checkout_url}\n\n"
                    body += "If you have already made the payment today, please ignore this automated email.\n\n"
                    body += "Best regards,\nPgAdmin Management"
                    
                    # 7. Send Email
                    try:
                        sender_email = settings.EMAIL_HOST_USER
                        email_msg = EmailMessage(
                            subject=subject,
                            body=body,
                            from_email=sender_email, 
                            to=[email],
                        )
                        email_msg.send(fail_silently=False)
                        emails_sent_count += 1
                        print(f"Bulk Sweep: Sent reminder to {email} ({name})")
                    except Exception as e:
                        print(f"Bulk Sweep Error: Failed to send to {email} - {e}")
                
                print(f"Bulk Sweep Complete. Dispatched {emails_sent_count} reminders for today ({today.strftime('%Y-%m-%d')}).")

            except Exception as e:
                print(f"Bulk Sweep Error: {str(e)}")

        # Start the sweep in a background thread so the pinging cron service doesn't timeout
        threading.Thread(target=run_sweep_in_background).start()

        return Response({
            "message": "Automated sweep initiated in the background.", 
            "status": "Running"
        }, status=status.HTTP_202_ACCEPTED)

class GeneratePaymentLinkView(APIView):
    """
    GET API to generate UPI payment deep links for a specific member.
    The frontend can use these links to directly open GPay, PhonePe, or Paytm 
    with the exact rent amount and payee pre-filled.
    
    Query Params:
    - member_id: The ID of the member to fetch rent for.
    """
    def get(self, request):
        import os
        from urllib.parse import urlencode

        member_id = request.query_params.get("member_id")
        if not member_id:
            return Response({"error": "member_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL")
        if not DATABASE_URL:
            return Response({"error": "FIREBASE_DATABASE_URL not set."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        try:
            # Fetch member details to get their rent amount
            res = http_session.get(f"{DATABASE_URL}/members/{member_id}.json", timeout=5)
            if res.status_code != 200 or not res.json():
                return Response({"error": "Member not found in database."}, status=status.HTTP_404_NOT_FOUND)
            
            member_data = res.json()
            rent_amt_str = str(member_data.get("monthly_rent", "0"))
            
            try:
                rent_amt = float(rent_amt_str)
            except ValueError:
                rent_amt = 0.0

            if rent_amt <= 0:
                return Response({"error": "Rent amount is zero or invalid."}, status=status.HTTP_400_BAD_REQUEST)

            # --- UPI Configuration ---
            # Using your provided mobile number (7359377502).
            # Note: A real UPI ID (VPA) is usually required by most UPI apps (e.g. 7359377502@paytm).
            upi_id = "7359377502@paytm" 
            payee_name = "PgAdmin"
            
            # The core query parameters required for a UPI transaction
            upi_params = {
                "pa": upi_id,                # Payee Address (UPI ID)
                "pn": payee_name,            # Payee Name
                "am": f"{rent_amt:.2f}",     # Amount (e.g. 8000.00)
                "cu": "INR",                 # Currency
                "tn": f"Rent payment for {member_data.get('name', 'Member')}" # Transaction Note
            }
            
            query_string = urlencode(upi_params)

            # Fetch PG Info to resolve names
            pg_id = member_data.get("pg_id", "")
            room_id = member_data.get("room_id", "")
            pg_name = pg_id
            room_number = room_id
            
            if pg_id:
                pg_res = http_session.get(f"{DATABASE_URL}/pg_properties/{pg_id}.json", timeout=5)
                if pg_res.status_code == 200 and pg_res.json():
                    pg_info = pg_res.json()
                    pg_name = pg_info.get("property_name", pg_info.get("pg_name", pg_info.get("name", pg_id)))
                    
                    if room_id and "rooms" in pg_info and room_id in pg_info["rooms"]:
                        room_info = pg_info["rooms"][room_id]
                        room_number = room_info.get("room_number", room_info.get("room_name", room_info.get("name", room_id)))

            member_name = member_data.get("full_name", member_data.get("name", "Unknown"))

            # Different apps have different deep link prefixes
            # The standard 'upi://pay' works on most mobile browsers to open the default UPI app chooser.
            links = {
                "generic_upi": f"upi://pay?{query_string}",
                "google_pay": f"gpay://upi/pay?{query_string}",
                "phonepe": f"phonepe://pay?{query_string}",
                "paytm": f"paytmmp://pay?{query_string}",
            }

            return Response({
                "member_name": member_name,
                "pg_name": pg_name,
                "room_number": room_number,
                "rent_amount": rent_amt,
                "due_date": member_data.get("rent_due_date", ""),
                "payment_links": links
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

from .cloudinary_client import upload_image, upload_pdf

class SubmitPaymentProofView(APIView):
    """
    POST API to submit payment proof (screenshot) and transaction ID.
    The frontend should extract the transaction ID (via OCR) and send it here
    along with the image file.
    
    Expected Form Data:
    - member_id (string)
    - transaction_id (string)
    - screenshot (file)
    """
    def post(self, request):
        member_id = request.data.get("member_id")
        transaction_id = request.data.get("transaction_id") or request.data.get("txn_id")
        screenshot_file = request.FILES.get("screenshot") or request.FILES.get("file") or request.FILES.get("image") or request.FILES.get("proof_image")

        print("--- PAYMENT PROOF DEBUG ---")
        print("Data Keys Received:", list(request.data.keys()))
        print("File Keys Received:", list(request.FILES.keys()))

        missing = []
        if not member_id: missing.append("member_id")
        if not transaction_id: missing.append("transaction_id")
        if not screenshot_file: missing.append("screenshot (must be a File upload)")

        if missing:
            return Response(
                {"error": f"Missing required fields: {', '.join(missing)}"}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL")
        if not DATABASE_URL:
            return Response({"error": "FIREBASE_DATABASE_URL not set."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # 1. Fetch member to get pg_id, name, rent amount
        res = http_session.get(f"{DATABASE_URL}/members/{member_id}.json", timeout=5)
        if res.status_code != 200 or not res.json():
            return Response({"error": "Member not found."}, status=status.HTTP_404_NOT_FOUND)
            
        member_data = res.json()
        pg_id = member_data.get("pg_id", "")
        rent_amt = member_data.get("monthly_rent", 0)

        # 2. Upload screenshot to Cloudinary
        secure_url = upload_image(screenshot_file, folder="rent_proofs")
        if not secure_url:
            return Response({"error": "Failed to upload screenshot to Cloudinary."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # 3. Create rent record in Firebase
        rent_record = {
            "member_id": member_id,
            "pg_id": pg_id,
            "transaction_id": transaction_id,
            "screenshot_url": secure_url,
            "monthly_rent": rent_amt,
            "status": "Under Review",
            "created_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
            "updated_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
        }

        # Update rent_records
        patch_res = http_session.patch(f"{DATABASE_URL}/rent_records/{member_id}.json", json=rent_record, timeout=5)
        if patch_res.status_code != 200:
            return Response({"error": "Failed to save rent record to database."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Also store in payments for historical records
        http_session.post(f"{DATABASE_URL}/payments.json", json=rent_record, timeout=5)

        return Response({
            "message": "Payment proof submitted successfully.",
            "record_id": member_id,
            "screenshot_url": secure_url
        }, status=status.HTTP_201_CREATED)

import PyPDF2

class UploadPaymentStatementView(APIView):
    """
    POST API to upload a PDF statement (e.g. bank statement).
    The PDF is stored in Cloudinary. The API extracts text from the PDF,
    checks for matching transaction IDs from 'Under Review' rent records,
    and updates their status to 'Paid' while attaching the statement URL.
    
    Expected Form Data:
    - statement (file, PDF)
    """
    def post(self, request):
        statement_file = request.FILES.get("statement") or request.FILES.get("file") or request.FILES.get("pdf")

        if not statement_file:
            return Response(
                {"error": "Missing required field: statement (must be a PDF upload)"}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL")
        if not DATABASE_URL:
            return Response({"error": "FIREBASE_DATABASE_URL not set."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # 1. Extract text from PDF
        pdf_text = ""
        try:
            pdf_reader = PyPDF2.PdfReader(statement_file)
            for page in pdf_reader.pages:
                text = page.extract_text()
                if text:
                    pdf_text += text + "\n"
            # Reset file pointer for Cloudinary upload
            statement_file.seek(0)
        except Exception as e:
            return Response({"error": f"Failed to parse PDF: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

        # 2. Upload PDF to Cloudinary
        secure_url = upload_pdf(statement_file, folder="payment_statements")
        if not secure_url:
            return Response({"error": "Failed to upload statement to Cloudinary."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # 3. Fetch all rent records and payments from Firebase
        urls = {
            "rent_records": f"{DATABASE_URL}/rent_records.json",
            "payments": f"{DATABASE_URL}/payments.json"
        }
        nodes = fetch_nodes_parallel(urls)
        rent_records = nodes.get("rent_records") or {}
        payments = nodes.get("payments") or {}

        matched_members = []
        updated_records = 0

        current_time = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()

        # 4. Check for matching transaction IDs and update
        for member_id, record in rent_records.items():
            txn_id = record.get("transaction_id")
            # We match if the txn_id is present in the statement
            # It's better to verify records that are not already Paid, but we can do it for any valid txn_id
            if txn_id and str(txn_id) in pdf_text:
                if record.get("status") != "Paid":
                    # Update rent_record
                    update_data = {
                        "status": "Paid",
                        "statement_url": secure_url,
                        "statement_uploaded_at": current_time,
                        "updated_at": current_time
                    }
                    patch_res = http_session.patch(f"{DATABASE_URL}/rent_records/{member_id}.json", json=update_data, timeout=5)
                    
                    if patch_res.status_code == 200:
                        matched_members.append(member_id)
                        updated_records += 1

                    # Update corresponding payment in payments node
                    for p_key, p_val in payments.items():
                        if p_val.get("member_id") == member_id and p_val.get("transaction_id") == txn_id:
                            http_session.patch(f"{DATABASE_URL}/payments/{p_key}.json", json=update_data, timeout=5)

        return Response({
            "message": "Payment statement processed successfully.",
            "statement_url": secure_url,
            "matched_members": matched_members,
            "updated_records": updated_records
        }, status=status.HTTP_201_CREATED)

