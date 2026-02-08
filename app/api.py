"""
Server Provisioning Webhook API endpoints.

This module provides FastAPI router with endpoints for processing webhook events
related to server resource provisioning and deprovisioning only.
STRICT MODE: Cleaned of legacy logging and logic.
"""
from typing import Optional, Union

from fastapi import APIRouter, Request, Header, HTTPException, status
from fastapi.responses import JSONResponse

from . import config, models, utils

logger = config.logger

# Constants for event types
EVENT_START = 'EVENT_START'
EVENT_END = 'EVENT_END'
EVENT_DELETED = 'EVENT_DELETED'

router = APIRouter()


@router.post("/webhook")
async def handle_webhook(
    payload: Union[models.WebhookPayload, models.EventWebhookPayload],
    request: Request, 
    x_webhook_signature: Optional[str] = Header(None)
) -> JSONResponse:
    """
    Handle incoming webhook events for server provisioning/deprovisioning.
    Only processes events for Server resource types.
    """
    # 1. Verifica Firma
    raw_payload = await utils.verify_webhook_signature(request, x_webhook_signature)
    
    # ------------------------------------------------------------------
    # CASO 1: Payload Standard (Start/End)
    # ------------------------------------------------------------------
    if isinstance(payload, models.WebhookPayload):
        logger.info(
            f"Processing Webhook Event: '{payload.event_type}', "
            f"User: '{payload.username}', Resource: '{payload.resource_name}'"
        )

        # Controllo Tipo Risorsa
        if payload.resource_type != "Server":
            logger.info(f"Skipping non-Server resource '{payload.resource_name}'.")
            return JSONResponse({
                "status": "success",
                "message": f"No action needed for resource type '{payload.resource_type}'."
            })

        # --- EVENTO START (Provisioning) ---
        if payload.event_type == EVENT_START:
            # Delega totale a utils. In Strict Mode, se torna False è perché mancano dati critici.
            if utils.handle_provision_event(payload, raw_payload):
                return utils.create_success_response("provision", payload.resource_name, payload.user_id)
            else:
                # ERRORE CRITICO (es. Manca URL Immagine).
                # Restituiamo 500 per segnalare il fallimento dell'operazione.
                logger.error(f"Provisioning failed for '{payload.resource_name}'.")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to provision server '{payload.resource_name}'. Missing config or K8s error."
                )

        # --- EVENTO END (Deprovisioning) ---
        elif payload.event_type == EVENT_END:
            if utils.handle_deprovision_event(payload, raw_payload):
                return utils.create_success_response("deprovision", payload.resource_name, payload.user_id)
            else:
                logger.error(f"Deprovisioning failed for '{payload.resource_name}'.")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to deprovision server '{payload.resource_name}'."
                )
        
        else:
            return JSONResponse({"status": "ignored", "message": f"Event '{payload.event_type}' not handled."})

    # ------------------------------------------------------------------
    # CASO 2: Payload Cancellazione (EventWebhookPayload)
    # ------------------------------------------------------------------
    elif isinstance(payload, models.EventWebhookPayload):
        if payload.event_type == EVENT_DELETED:
            res_name = payload.data.resource.name
            logger.info(f"Processing EVENT_DELETED for Resource: '{res_name}'")
            
            # Logica Temporale: Deprovisioning solo se la prenotazione era attiva ORA
            now = utils.parse_timestamp(payload.timestamp)
            start = utils.parse_timestamp(payload.data.start)
            end = utils.parse_timestamp(payload.data.end)

            # Se la cancellazione avviene DURANTE la prenotazione, spegni il server.
            if start <= now < end:
                logger.info(f"Reservation active. Initiating deprovision for '{res_name}'.")
                
                if utils.handle_deprovision_event(payload, raw_payload):
                    return JSONResponse({
                        "status": "success", 
                        "message": f"Deprovisioning initiated for '{res_name}' (Active Reservation Deleted)."
                    })
                else:
                    logger.error(f"Failed to deprovision '{res_name}' on DELETE event.")
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Failed to deprovision server '{res_name}'."
                    )
            else:
                logger.info(f"Reservation for '{res_name}' was not active. No deprovision needed.")
                return JSONResponse({
                    "status": "success",
                    "message": "No action taken (Reservation not active)."
                })
        
        else:
            return JSONResponse({"status": "ignored", "message": "Event type not handled."})

    else:
        logger.warning("Unknown payload structure received.")
        # Anche qui, se il payload è sconosciuto, potrebbe essere un errore del chiamante (400) o del server (500).
        # Manteniamo la coerenza con il return JSON per "unknown structure" ma loggiamo warning.
        return JSONResponse({"status": "error", "message": "Unknown payload structure."})


@router.get("/healthz")
def health_check() -> dict:
    """Health check endpoint."""
    return {"status": "healthy", "service": "server-provisioning-webhook"}