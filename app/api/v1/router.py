"""Aggregates every v1 endpoint module into one router."""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    activity,
    analytics,
    auth,
    balances,
    currencies,
    expenses,
    friends,
    groups,
    health,
    khata,
    khata_entries,
    loans,
    notes,
    people,
    reports,
    settlements,
    users,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(currencies.router)
api_router.include_router(auth.router)
# Must come before users.router: its "/users/{user_id}" would otherwise match
# "/users/search" first and reject "search" as a malformed UUID.
api_router.include_router(friends.search_router)
api_router.include_router(users.router)
api_router.include_router(friends.router)
api_router.include_router(groups.router)
api_router.include_router(expenses.router)
api_router.include_router(balances.router)
api_router.include_router(settlements.router)
# The entry router first: its `/khata/entries/{id}` would otherwise be matched
# by `/khata/{khata_id}`, which reads "entries" as a malformed uuid.
api_router.include_router(khata_entries.entry_router)
api_router.include_router(khata_entries.router)
api_router.include_router(khata.router)
# The payment router first, for the same reason: `/loans/payments/{id}` would
# otherwise be matched by `/loans/{loan_id}`.
api_router.include_router(loans.payment_router)
api_router.include_router(loans.router)
api_router.include_router(notes.notes_router)
api_router.include_router(notes.reminders_router)
api_router.include_router(people.router)
api_router.include_router(activity.router)
api_router.include_router(analytics.router)
api_router.include_router(reports.router)
