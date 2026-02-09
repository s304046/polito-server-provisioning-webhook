"""
Pydantic models for Server webhook payload validation.

This module defines the data models used for validating incoming webhook payloads.
STRICT MODE: Only accepts Dynamic Image Provisioning (URL-based).
Static OS slug mapping has been removed.
"""
from typing import Optional, List

from pydantic import BaseModel, Field


class WebhookPayload(BaseModel):
    """
    Model for webhook event payload (EVENT_START / EVENT_END).
    Handles a single Server event with multi-SSH key support.
    STRICT MODE: Requires explicit imageUrl and checksumUrl for provisioning.
    """
    # --- Identificatori Evento ---
    event_type: str = Field(..., alias='eventType', description="Type of the event (EVENT_START, EVENT_END)")
    timestamp: str = Field(..., description="Timestamp when the event occurred")
    event_id: str = Field(..., alias='eventId', description="Unique identifier for the event")
    webhook_id: int = Field(..., alias='webhookId', description="Unique identifier for the webhook")
    
    # --- Dati Utente ---
    user_id: Optional[str] = Field(None, alias='userId', description="ID of the user associated with the event")
    username: Optional[str] = Field(None, description="Username of the user")
    email: Optional[str] = Field(None, description="Email address of the user")
    
    # --- Gestione Accessi (SSH) ---
    # Accettiamo solo la lista di chiavi. Il campo singolo obsoleto è stato rimosso.
    ssh_keys: Optional[List[str]] = Field(default_factory=list, alias='sshKeys', description="List of SSH public keys")
    
    
    # --- LOGICA PROVISIONING: CAMBIAMENTI QUI ---
    image_url: Optional[str] = Field(None, alias='imageUrl')
    checksum_url: Optional[str] = Field(None, alias='checksumUrl')
    image_format: Optional[str] = Field(None, alias='imageFormat')

    # --- Dettagli Evento e Risorsa ---
    event_title: Optional[str] = Field(None, alias='eventTitle', description="Title of the reservation event")
    event_description: Optional[str] = Field(None, alias='eventDescription', description="Description of the event")
    event_start: str = Field(..., alias='eventStart', description="Start time of the event")
    event_end: str = Field(..., alias='eventEnd', description="End time of the event")
    
    custom_parameters: Optional[str] = Field(None, alias='customParameters', description="JSON serialized string of custom parameters")
    
    resource_id: int = Field(..., alias='resourceId', description="Identifier of the resource")
    resource_name: str = Field(..., alias='resourceName', description="Name of the resource (BareMetalHost name)")
    resource_type: str = Field(..., alias='resourceType', description="Type of the resource - must be 'Server'")
    resource_specs: Optional[str] = Field(None, alias='resourceSpecs', description="Specifications of the resource")
    resource_location: Optional[str] = Field(None, alias='resourceLocation', description="Location of the resource")
    
    site_id: Optional[str] = Field(None, alias='siteId', description="Identifier of the site")
    site_name: Optional[str] = Field(None, alias='siteName', description="Name of the site")

    class Config:
        populate_by_name = True  # Permette image_url = data['imageUrl']
        from_attributes = True   # Utile per compatibilità versioni Pydantic


class EventResourceInfo(BaseModel):
    """Model for resource information within EVENT_DELETED data."""
    name: str = Field(..., description="Name of the resource to be released")
    id: int = Field(..., description="Unique identifier for the resource")
    specs: Optional[str] = Field(None, alias='specs', description="Specifications of the resource")
    location: Optional[str] = Field(None, alias='location', description="Location of the resource")


class EventData(BaseModel):
    """Model for the 'data' field in an EVENT_DELETED payload."""
    id: int = Field(..., description="Unique identifier for the deletion event data")
    start: str = Field(..., description="Original start time of the reservation")
    end: str = Field(..., description="Original end time of the reservation")
    custom_parameters: Optional[str] = Field(None, alias='customParameters', description="JSON serialized string of custom parameters")
    resource: EventResourceInfo = Field(..., description="Details of the resource associated with the event")
    keycloak_id: Optional[str] = Field(None, alias='keycloakId', description="Keycloak ID of the user")


class EventWebhookPayload(BaseModel):
    """Model for EVENT_DELETED webhook payload."""
    event_type: str = Field(..., alias='eventType', description="Type of the event, should be EVENT_DELETED")
    timestamp: str = Field(..., description="Timestamp when the event occurred")
    webhook_id: str = Field(..., alias='webhookId', description="Unique identifier for the webhook")
    data: EventData = Field(..., description="Detailed data for the EVENT_DELETED event")