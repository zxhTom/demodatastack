from pydantic import BaseModel
from typing import Optional


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    user_id:      int
    username:     str
    role:         str


class UserInfo(BaseModel):
    id:        int
    username:  str
    email:     Optional[str]
    role:      str
    is_active: bool

    model_config = {"from_attributes": True}
