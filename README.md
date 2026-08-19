# 💸 Owsify

> **Track expenses, split bills, and settle group balances effortlessly.**

**Owsify** is an open-source expense-splitting platform engineered to simplify group finances. Whether managing household rent, trips with friends, or shared dining bills, Owsify eliminates awkward money conversations by automating group debt calculation, tracking individual balances, and simplifying complex settlements.

### ✨ Key Features

* **Smart Expense Splitting:** Split bills equally, by percentage, or by exact custom amounts.
* **Simplified Debt Graph:** Integrated balance minimization algorithms to reduce the total number of transactions needed to settle up.
* **Group Management:** Organise expenses by trip, household, or project with multi-currency support.
* **Real-time Balance Tracking:** Instantly see who owes whom at any given moment.
* **Activity & Ledger Logs:** Full historical audit trail of settled payments and edited transactions.

### 🛠️ Tech Stack

* **Frontend:** React / React Native (TypeScript)
* **Backend:** Node.js / Express (or your backend framework)
* **Database:** PostgreSQL / MongoDB


# Splitwise Clone — Backend

FastAPI + PostgreSQL + SQLAlchemy 2.0 + Alembic, with JWT authentication.

## Requirements

- Python 3.13
- PostgreSQL 14+

## Setup

```bash
cd backend
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env          # then edit the values
```

Generate a real secret key before doing anything beyond local work:

```bash
./venv/bin/python -c "import secrets; print(secrets.token_urlsafe(64))"
```

## Database

This project talks to a dedicated cluster on **port 5433** so it does not collide
with any other PostgreSQL install on the machine.

```bash
# One-time: create the cluster
initdb -D /usr/local/var/splitwise-pg -U dev --auth-local=trust --auth-host=trust -E UTF8

# Start it
pg_ctl -D /usr/local/var/splitwise-pg -o "-p 5433" -l /usr/local/var/splitwise-pg/server.log start

# One-time: role and databases
psql -h 127.0.0.1 -p 5433 -U dev -d postgres <<'SQL'
CREATE ROLE splitwise WITH LOGIN PASSWORD 'splitwise' CREATEDB;
CREATE DATABASE splitwise OWNER splitwise;
CREATE DATABASE splitwise_test OWNER splitwise;
SQL
```

Stop it with `pg_ctl -D /usr/local/var/splitwise-pg stop`.

## Migrations

```bash
./venv/bin/alembic upgrade head              # apply
./venv/bin/alembic revision --autogenerate -m "message"
./venv/bin/alembic downgrade -1              # roll back one
./venv/bin/alembic current                   # what is applied
```

`alembic/env.py` pulls the URL and the model metadata from `app.core.config`, so
migrations always target the same database the app uses.

## Run

```bash
./venv/bin/uvicorn app.main:app --reload
```

| URL | What |
| --- | --- |
| <http://localhost:8000/docs> | Swagger UI (has a working **Authorize** button) |
| <http://localhost:8000/redoc> | ReDoc |
| <http://localhost:8000/api/v1/health> | Health + database check |

## Tests

```bash
./venv/bin/pytest
```

Tests run against `splitwise_test`; each one is wrapped in a transaction that is
rolled back, so the suite leaves no rows behind.

## Layout

```
app/
  core/          config, security (hashing + JWT), error types and handlers
  db/            declarative base, engine, session dependency
  models/        SQLAlchemy models
  schemas/       Pydantic request/response models
  services/      business logic, kept out of the route handlers
  api/
    deps.py      DbSession, CurrentUser, ActiveUser, SuperUser
    v1/          the versioned router and its endpoint modules
alembic/         migration environment and versions
tests/           pytest suite
```


### Permissions

- A **group** is invisible to non-members: they get 404, not 403, so ids cannot be
  probed for existence.
- Group **owners** can delete the group and change roles. **Admins** can edit it and
  manage members. The owner cannot leave without transferring ownership first.
- A member who appears in any of the group's expenses cannot be removed until those
  expenses are dealt with, so nobody's debt silently disappears.
- An **expense** can be edited or deleted by whoever added it, whoever paid, or a
  group admin.
- Everyone on a group expense must be in that group. Everyone on a personal expense
  must be a friend, so debt cannot be pushed onto a stranger.

### Errors

Every failure uses one envelope, so clients only parse one shape:

```json
{
  "error": {
    "code": "invalid_credentials",
    "message": "Incorrect email or password.",
    "details": [{ "field": "email", "message": "…", "type": "value_error" }]
  },
  "request_id": "7131d919a311487b8e714d0da7e8fd92"
}
```

`request_id` is also returned as the `X-Request-ID` header and is written to the
log line for the same request.

### Tokens

- **Access token** — stateless JWT, 30 minutes by default, carries `type: access`.
- **Refresh token** — JWT carrying `type: refresh` and a `jti` mirrored by a row
  in `refresh_tokens`, which is what allows revocation.

Refreshing **rotates**: the presented token is revoked and a new pair issued. If a
revoked token is presented again the whole user's sessions are dropped, since a
replay is the signature of a stolen token. An access token is never accepted where
a refresh token is expected, or the other way round.
