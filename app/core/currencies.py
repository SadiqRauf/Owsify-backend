"""Supported currency codes.

Validating against the ISO 4217 active list stops typos like "USDD" or "AAA" from
reaching the database, where they would render as broken amounts forever. The set
is a published standard, so it does not drift the way a hand-picked list would.

The frontend shows a curated subset of these in its dropdown; because that subset
is drawn from the same standard, the two can never disagree about validity.
"""

from __future__ import annotations

# Active ISO 4217 alphabetic codes.
ISO_4217_CODES: frozenset[str] = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN
    BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD
    CAD CDF CHF CLP CNY COP CRC CUP CVE CZK
    DJF DKK DOP DZD
    EGP ERN ETB EUR
    FJD FKP
    GBP GEL GHS GIP GMD GNF GTQ GYD
    HKD HNL HRK HTG HUF
    IDR ILS INR IQD IRR ISK
    JMD JOD JPY
    KES KGS KHR KMF KPW KRW KWD KYD KZT
    LAK LBP LKR LRD LSL LYD
    MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN
    NAD NGN NIO NOK NPR NZD
    OMR
    PAB PEN PGK PHP PKR PLN PYG
    QAR
    RON RSD RUB RWF
    SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL
    THB TJS TMT TND TOP TRY TTD TWD TZS
    UAH UGX USD UYU UZS
    VES VND VUV
    WST
    XAF XCD XOF XPF
    YER
    ZAR ZMW ZWG
    """.split()
)

DEFAULT_CURRENCY = "USD"


def normalise_currency(value: str) -> str:
    """Upper-case and validate a currency code, raising ValueError if unknown."""
    code = value.strip().upper()
    if code not in ISO_4217_CODES:
        raise ValueError(f"{value!r} is not a recognised currency code.")
    return code
