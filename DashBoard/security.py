import jwt
import hashlib
from datetime import datetime, timedelta, timezone
from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

JWT_SECRET = settings.SECRET_KEY
JWT_ALGORITHM = "HS256"

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def create_token(data: dict, expires_delta: timedelta):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + expires_delta
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return encoded_jwt

def verify_token(token: str):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload, None
    except jwt.ExpiredSignatureError:
        return None, "expired"
    except jwt.PyJWTError:
        return None, "invalid"

class JWTAuthentication(BaseAuthentication):
    def authenticate(self, request):
        # First try to get token from Authorization header
        auth_header = request.META.get('HTTP_AUTHORIZATION')
        token = None
        
        if auth_header:
            parts = auth_header.split()
            if len(parts) == 2 and parts[0].lower() == 'bearer':
                token = parts[1]
                
        # If not in header, try cookies
        if not token:
            token = request.COOKIES.get('access_token')
            
        if not token:
            return None
            
        payload, error = verify_token(token)
        if error == "expired":
            raise AuthenticationFailed('Token has expired', code='token_expired')
        if error == "invalid" or not payload:
            raise AuthenticationFailed('Invalid token', code='token_not_valid')
            
        # Ensure it's an access token
        if payload.get("type") != "access":
            raise AuthenticationFailed('Invalid token type', code='invalid_type')
            
        return (payload, token)
