"""Model, API key and feature-switch settings for the local workspace. Keys are write-only."""
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from eregion.domain import model_settings as settings

router = APIRouter(prefix='/api/settings/model', tags=['settings'])
LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1'}
ENV_PATH = settings.DEFAULT_ENV_PATH


def local_workspace(request: Request):
    # The key reaches the provider, so only the local workspace may read or change these settings:
    # a loopback host name (not a rebound DNS name), the page's own origin and a loopback client.
    origin = request.headers.get('origin')
    client = request.client.host if request.client else ''
    if (request.url.hostname not in LOCAL_HOSTS or (origin and urlsplit(origin).netloc != request.url.netloc)
            or client not in {*LOCAL_HOSTS, 'testclient'}):
        raise HTTPException(403, 'Open model settings from the local workspace.')


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model_id: str | None = Field(default=None, max_length=300)
    fast_model_id: str | None = Field(default=None, max_length=300)
    extraction_model_id: str | None = Field(default=None, max_length=300)
    base_url: str | None = Field(default=None, max_length=300)
    api_key: str | None = Field(default=None, max_length=500)
    features: dict[str, bool | str | None] | None = Field(default=None, max_length=40)


class ModelListRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    api_key: str | None = Field(default=None, max_length=500)
    base_url: str | None = Field(default=None, max_length=300)


@router.get('', dependencies=[Depends(local_workspace)])
def read_settings():
    return settings.read_settings(env_path=ENV_PATH)


@router.put('', dependencies=[Depends(local_workspace)])
def save_settings(body: SettingsUpdate):
    try:
        return settings.save_settings(body.model_dump(), env_path=ENV_PATH)
    except settings.SettingsLocked as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('/models', dependencies=[Depends(local_workspace)])
def list_models(body: ModelListRequest):
    try:
        return {'models': settings.list_models(api_key=body.api_key, base_url=body.base_url, env_path=ENV_PATH)}
    except settings.ModelListError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
