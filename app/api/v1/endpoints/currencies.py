"""The currency codes this API accepts."""

from fastapi import APIRouter

from app.core.currencies import DEFAULT_CURRENCY, ISO_4217_CODES
from app.schemas.common import CurrencyList

router = APIRouter(tags=["reference"])


@router.get("/currencies", response_model=CurrencyList, summary="Accepted currency codes")
def list_currencies() -> CurrencyList:
    """Every ISO 4217 code the API will store, so a client can validate before posting."""
    return CurrencyList(default=DEFAULT_CURRENCY, codes=sorted(ISO_4217_CODES))
