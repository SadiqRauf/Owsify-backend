"""Aggregates every v1 endpoint module into one router."""

from fastapi import APIRouter

from app.api.v1.endpoints import auth, currencies, expenses, friends, groups, health, users

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
