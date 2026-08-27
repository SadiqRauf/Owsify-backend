# 💸 Owsify

> **Track expenses, split bills, and settle group balances effortlessly.**

**Owsify** is an open-source expense-splitting platform engineered to simplify group finances. Whether managing household rent, trips with friends, or shared dining bills, Owsify eliminates awkward money conversations by automating group debt calculation, tracking individual balances, and simplifying complex settlements.

### ✨ Key Features

* **Smart Expense Splitting:** Split bills equally, by percentage, or by exact custom amounts.
* **Simplified Debt Graph:** Integrated balance minimization algorithms to reduce the total number of transactions needed to settle up.
* **Group Management:** Organise expenses by trip, household, or project with multi-currency support.
* **Real-time Balance Tracking:** Instantly see who owes whom at any given moment.
* **Khata:** A private running ledger for one person, who does not need an account.
* **Loans:** A fixed principal in either direction, paid down by its repayments.
* **Notes, Reminders & Timeline:** The context around a balance, and one history per person.
* **Reports:** Money in and out over any window, per currency, with charts.
* **Activity & Ledger Logs:** Full historical audit trail of settled payments and edited transactions.

### 🛠️ Tech Stack

* **Frontend:** React 19 + TypeScript + Vite, Tailwind, TanStack Query
* **Backend:** Python 3.13 + FastAPI + SQLAlchemy 2.0 + Alembic
* **Database:** PostgreSQL 14+


## 🚀 Running locally

Three processes: PostgreSQL, the API, and the web app. Start them in that order —
the API needs a database to migrate against, and the web app is useless without the
API. Locally you apply migrations yourself, in step 2; only the Docker image runs
them for you.

### 0. Prerequisites

| Tool | Version | Check with |
| --- | --- | --- |
| Python | 3.13 | `python3 --version` |
| Node | 20+ | `node --version` |
| PostgreSQL | 14+ | `postgres --version` |

### 1. Database

This project uses a **dedicated cluster on port 5433**, so it cannot collide with
any other PostgreSQL already on the machine. One-time setup:

```bash
initdb -D /usr/local/var/splitwise-pg -U dev --auth-local=trust --auth-host=trust -E UTF8

pg_ctl -D /usr/local/var/splitwise-pg -o "-p 5433" -l /usr/local/var/splitwise-pg/server.log start

psql -h 127.0.0.1 -p 5433 -U dev -d postgres <<'SQL'
CREATE ROLE splitwise WITH LOGIN PASSWORD 'splitwise' CREATEDB;
CREATE DATABASE splitwise OWNER splitwise;
CREATE DATABASE splitwise_test OWNER splitwise;
SQL
```

Every time after that, just start it:

```bash
pg_ctl -D /usr/local/var/splitwise-pg -o "-p 5433" start
```

Stop it with `pg_ctl -D /usr/local/var/splitwise-pg stop`.

### 2. Backend — <http://localhost:8000>

```bash
cd backend
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp .env.example .env
# Generate a real signing key and paste it in as SECRET_KEY:
./venv/bin/python -c "import secrets; print(secrets.token_urlsafe(64))"

./venv/bin/alembic upgrade head     # create the tables
./venv/bin/uvicorn app.main:app --reload
```

The defaults in `.env.example` already point at the cluster above, so the only
value you need to change is `SECRET_KEY`.

### 3. Frontend — <http://localhost:5173>

In a second terminal:

```bash
cd frontend
npm install
cp .env.example .env     # already points at http://localhost:8000/api/v1
npm run dev
```

### 4. Check it works

Open <http://localhost:5173>, register an account, and you should land on the
dashboard. If the page loads but every request fails, the API is not running or
`VITE_API_URL` does not match where it is listening.

| URL | What |
| --- | --- |
| <http://localhost:5173> | The web app |
| <http://localhost:8000/docs> | Swagger UI, with a working **Authorize** button |
| <http://localhost:8000/api/v1/health> | Health + database connectivity |

### Everything at once, with Docker

If you would rather not install Python, Node and Postgres separately:

```bash
docker compose up --build      # from the repository root
```

Three containers: `db`, `api` and `web`. The entrypoint waits for PostgreSQL and
runs `alembic upgrade head` before the API starts, so replicas cannot race a
half-applied schema.

| URL | What |
| --- | --- |
| <http://localhost:8080> | The web app (**not** 5173 — that is the Vite dev server) |
| <http://localhost:8000> | The API |

Override the ports with `WEB_PORT`, `API_PORT` and `POSTGRES_PORT`. See
[Production](#production) for what the image does differently from the dev setup.

### Troubleshooting

| Symptom | Cause |
| --- | --- |
| `connection refused` on port 5433 | The cluster is not running — `pg_ctl … start` |
| `password authentication failed` | `POSTGRES_*` in `backend/.env` disagrees with the role you created |
| API starts but every request 500s | Migrations not applied — `alembic upgrade head` |
| Web app loads, all requests fail | API not running, or `VITE_API_URL` points somewhere else |
| `SECRET_KEY` startup crash | Only in `ENVIRONMENT=production`; generate a real key |

---

# Owsify — Backend

FastAPI + PostgreSQL + SQLAlchemy 2.0 + Alembic, with JWT authentication.

The rest of this file is backend reference: how each subsystem works and why it is
built that way. For getting the app running, the section above is all you need.

## Requirements

- Python 3.13
- PostgreSQL 14+

Installing and starting both is covered in [Running locally](#-running-locally)
above; it is not repeated here, so there is only ever one set of instructions to
keep correct.

## Configuration

`.env` is git-ignored; `.env.example` is the documented template and lists every
setting with its default. The values that matter most:

| Setting | Default | Notes |
| --- | --- | --- |
| `SECRET_KEY` | a placeholder | Must be replaced. Signs every token. |
| `POSTGRES_PORT` | `5433` | The dedicated cluster, not a system-wide 5432 |
| `ENVIRONMENT` | `development` | `production` enables the startup safety checks |
| `BCRYPT_ROUNDS` | `12` | The test suite drops this to 4 for speed |
| `CORS_ORIGINS` | the Vite dev server | Comma-separated; `*` is refused in production |

Either set the discrete `POSTGRES_*` values or set `DATABASE_URL` to override them
all.

## Sending email

Four backends, chosen with `EMAIL_BACKEND`:

| Backend | What it does | Use when |
| --- | --- | --- |
| `console` | Logs the message | Default, and what the test suite forces |
| `file` | Writes `.eml` files to `EMAIL_FILE_PATH` | You want to click the link locally |
| `smtp` | Actually sends | Everything else |

`send_email` never raises. A failed send is logged and reported as `False`, because
an outage in the mail provider must not take a request down with it.

The test suite **forces** `console` in `conftest.py` rather than defaulting to it.
The developer `.env` sets `smtp`, and without the override every test that sends mail
opened a real connection to the provider — slow, network-dependent, and a source of
genuine outbound mail from a test run.

### Gmail

Gmail works through the ordinary SMTP backend, but two things about it fail silently
and are handled for you.

**It needs an App Password, not your password.** Google stopped accepting account
passwords over SMTP in 2022, and an App Password requires 2-Step Verification first:

1. Turn on 2-Step Verification — <https://myaccount.google.com/signinoptions/two-step-verification>
2. Create an App Password — <https://myaccount.google.com/apppasswords>
3. Put the 16 characters in `SMTP_PASSWORD`

```bash
EMAIL_BACKEND=smtp
EMAIL_FROM="Owsify <you@gmail.com>"
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com          # the full address
SMTP_PASSWORD=abcdefghijklmnop   # the App Password
SMTP_USE_TLS=true
SMTP_USE_SSL=false
```

Port 465 works too, with the flags the other way round (`SMTP_USE_SSL=true`,
`SMTP_USE_TLS=false`). Both are verified to connect and negotiate TLS.

Google displays an App Password as `abcd efgh ijkl mnop`. Pasted verbatim the spaces
break the login, and the only feedback is a bare "Username and Password not
accepted" that never mentions them — so **spaces are stripped for Gmail hosts**.
Only Gmail's, because another provider could legitimately have a space in a
password.

**Gmail only sends as the account you authenticated with.** If `EMAIL_FROM` is any
other address, the app rewrites it to `SMTP_USER` and logs a warning. Doing it here
rather than leaving it means the address the app believes it used is the address that
actually goes out — otherwise Google either rewrites the header itself, silently, or
refuses the message. To send as something else, add it under Gmail → Settings →
Accounts and Import → *Send mail as* first.

Free Gmail allows roughly 500 recipients a day and Workspace 2,000. That is fine for
development and light use; past it, a transactional provider is the answer.

### When it does not work

```bash
./venv/bin/python -m app.cli.send_test_email you@example.com
```

This prints the live configuration and turns the failure into a next step. SMTP
errors are famously opaque — Google answers every credential problem with the same
"Username and Password not accepted" — so the tool works out which cause applies
from the config and says so:

```
Google rejected the credentials (code 535).
  5.7.8 Username and Password not accepted. …

  → SMTP_PASSWORD is 13 characters. A Google App Password is exactly 16 — if this
    is your normal Gmail password, Google has refused those since 2022.
```

The ordering inside `_diagnose` is deliberate and not obvious: both `SMTPException`
and `SSLError` subclass `OSError`, so the specific cases must be matched before any
generic connection branch or they get the wrong explanation.

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

Both docs routes are disabled when `ENVIRONMENT=production`.

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


### The balance engine

`app/services/balance.py` is where every statement about who owes whom comes from.
Three properties are maintained deliberately, because breaking any of them shows up
as money that has silently appeared or vanished:

1. **Integer cents.** No float touches a balance. Decimal in, cents through the
   arithmetic, Decimal out.
2. **Per currency.** Balances are never summed across currencies. Two people can owe
   each other in USD and EUR simultaneously and both are true; adding them would
   produce a number with no meaning. There is deliberately no combined total.
3. **Zero sum.** Within one currency and scope, every ledger sums to zero.
   `Ledger.assert_consistent()` states this and the test suite exercises it against
   randomly generated webs of debt.

The ledger stores **one signed number per pair** rather than two directed ones, so
the two directions cannot disagree. It is recomputed from the transactions on each
request rather than cached on a row: a stored balance drifts from its transactions
after an edit or delete, and a wrong balance that looks authoritative is worse than
a slow one.

**Debt simplification** (`GET /balances/groups/{id}/simplified`) reduces a web of
debts to the fewest transfers that settle everyone, by repeatedly matching the
largest debtor with the largest creditor. For the brief's example:

    Ali owes Sadiq $50, Ahmed owes Ali $30
    -> nets: Sadiq +50, Ali -20, Ahmed -30
    -> Ahmed pays Sadiq $30, Ali pays Sadiq $20

This deliberately reassigns who pays whom — Ahmed never borrowed from Sadiq
directly — which is why it is a separate view rather than a replacement for the
real pairwise debts.

### Settlements

A settlement is not an expense. An expense creates debt and divides between people;
a settlement discharges debt and moves money one way. They are separate tables so
no balance query has to remember which rows to treat differently.

Deleting a settlement restores the debt it discharged, because the balance is
derived rather than stored.

### Activity

`GET /activity` merges expenses and settlements into one feed, derived from those
tables rather than an append-only log. A derived feed can never describe something
that no longer exists; the cost is that only current state is visible, so a deleted
expense leaves the feed and edits show new values rather than a change history.

### Analytics

Two decisions shape `app/services/analytics.py`:

**"Spending" means your share, not what you paid.** The figures join through
`expense_splits`, not `expenses.paid_by_id`. Someone who fronts the money for a
group has not spent it all.

**One currency per query.** Every analytics call is scoped to a single currency
(the caller's default unless they say otherwise). Balances still span currencies, so
a single-currency dashboard never hides money.

`spending_series` handles both granularities through one query and one gap-filling
loop, so a daily and a monthly view of the same window cannot disagree about the
total. Empty buckets are returned as zero rather than omitted, so a chart gets an
even time axis instead of silently compressing gaps. `count` defaults to 30 days or
6 months, and each granularity clamps to its own maximum (366 days, 60 months).

### Khata

A khata is a running two-party ledger — **one person's book about one other
person**. That is a different shape from a group, and the difference drives three
decisions:

**The other party need not have an account.** The main use of a khata is a
shopkeeper keeping one for a customer who will never install anything, so a khata
carries its own `person_name` and only *optionally* links to a `User`. Requiring
the counterparty to sign up first would remove the feature's main case.

**It belongs to its owner alone.** Two people who deal with each other each keep
their own book; neither can read the other's. A khata that is not yours returns
404, not 403 — confirming it exists would leak who someone deals with.

**Archiving is the default removal.** `DELETE` archives and keeps the history;
`DELETE ?permanent=true` destroys the ledger and its entries. For a financial
record the forgiving action belongs on the unqualified verb.

Balances are summed from `khata_entries` on read, never stored on the account row,
for the same reason group balances are. Entry direction is `GIVEN` / `RECEIVED`
rather than debit / credit: those two inverting depending on whose books you think
you are in is exactly the confusion a ledger cannot afford.

**Entries are the source of truth.** No balance is stored anywhere: every figure
the API reports is summed from `khata_entries` at read time, so an edited or
deleted entry can never leave a total behind that disagrees with the book.

There are three entry types. `GIVEN` and `RECEIVED` carry direction in the type, so
their amounts stay positive — a negative `GIVEN` and a positive `RECEIVED` would be
two ways to write one fact. `ADJUSTMENT` is a correction, has no inherent
direction, and is the only type whose amount may be negative. Two CHECK constraints
enforce exactly that (`amount <> 0`, and `entry_type = 'adjustment' OR amount > 0`),
with the same rules restated in the service so the user gets a field error rather
than a 500 from a constraint violation.

`running_balance` is computed with a **window function over the khata's whole
history**, and the page is taken from that result. Summing only the rows on the page
would restart the total at every page boundary, and a date filter would restart it
mid-history. The number has to mean "the balance after this entry", not "the balance
after this entry among the rows you can see" — for the same reason, the page's
`balance` and `totals` describe the khata and ignore the filters, so a filtered view
can never make an unsettled khata look settled.

Entries are created and listed under their khata but addressed directly once they
exist (`PATCH`/`DELETE /khata/entries/{id}`). That router is registered **before**
`/khata/{khata_id}`, which would otherwise read `entries` as a malformed UUID.

**Attachments are not built.** The brief lists a receipt photo on an entry; nothing
in the app stores files — no object storage, no upload endpoint, no way to serve one
back — so the form says so rather than offering an input that silently drops what it
takes.

### People

`GET /people/{user_id}/summary` is the one place the app adds a person up across
every subsystem: group expenses, settlements, khata, and loans. It asks the existing
services rather than re-deriving anything — the group figure comes from the same
balance engine the group pages use, so the two can never disagree about the same
relationship.

Every component is signed the same way — **positive means they owe you** — so the
total is a plain sum, and everything is scoped to one currency because adding PKR to
USD is arithmetic on incompatible units.

`loan_balance` is the outstanding total across the loans you have given them,
cancelled loans excluded — a written-off loan is not money you expect back. It was a
stated zero until the loan feature existed; see **Loans** below.

`GET /people/{user_id}/activity` merges expenses, settlements and khata entries into
one feed. `GET /people` lists everyone you share money with — and, marked
`has_account: false`, the khata contacts who have no account at all. They cannot
have a person page, since `/people/{id}` is keyed by user id, so their row points at
their khata instead. Hiding them would be worse: from the owner's side they are
exactly as real as anyone else.

### Loans

A loan is the same shape as a khata — one person's record about one other person —
but it answers a different question. A khata is an open-ended running tally with no
end state; a loan is a fixed principal that is either outstanding or settled, with a
date by which it should have been settled.

**A loan has a direction.** `GIVEN` means you lent it and they owe you; `TAKEN`
means you borrowed it and you owe them. The row is always owned by whoever keeps the
record, so the columns are `owner_id` and `counterparty_*` rather than lender and
borrower — for a `TAKEN` loan the owner *is* the borrower, and a column named
`lender_id` holding a borrower would be a schema that lies about its own rows. The
migration renames rather than adds-and-copies, because every pre-existing row is a
loan that was given and needs no transformation.

`signed_balance` is the one number that matters, and it is signed the same way as
every other balance in the app: **positive means they owe you**. It is
`(amount − paid)` for a loan you gave and the negation of that for one you took, so
loans fold into khata and group balances with no special case at the call site.

**Status is derived, not stored.** The model has no `status` column. Four of the five
statuses are facts about the payments and the calendar — `PAID` means the payments
add up to the principal, `OVERDUE` means the due date passed with money outstanding —
and a stored copy of a derived fact is a copy that can go stale. A row saying PAID
while its payments sum to less is a loan nobody can trust. Only `CANCELLED` is a
decision rather than a consequence, so only that one is a column (`cancelled_at`).

`OVERDUE` takes precedence over `PARTIALLY_PAID`: a part-paid loan that is late is
late, and showing it as merely part-paid buries the fact that needs acting on. A
settled loan is never overdue — there is nothing left to be late for.

**"Mark as paid" records a payment**, via `POST /loans/{id}/settle`, rather than
setting a flag. A loan marked PAID whose payments add up to less is exactly the
inconsistency this design avoids, so the closing payment is written for whatever is
left, and the history then shows what closed the loan and when.

**Overpayment flips the balance.** Repay 1,500 against a 1,000 loan and the extra
500 is owed the other way: `remaining` is 0, `overpaid` is 500, `signed_balance` is
−500, and the status becomes `OVERPAID`.

`OVERPAID` is a sixth status beyond the five in the brief, and a separate one from
`PAID` on purpose — the two mean opposite things about who owes whom. `PAID` means
nobody owes anybody; `OVERPAID` means the debt now runs the other way, and calling
it paid would hide money that is genuinely owed. An earlier version clamped
`remaining` at zero and quietly lost that 500.

The principal cannot be lowered below what has already been repaid — that would
describe a loan its own history contradicts. `DELETE` cancels and keeps the payments;
`?permanent=true` destroys both, the same convention as khata.

Status is filtered in Python rather than SQL. Three of the five values depend on the
payment total and today's date, so expressing them as SQL predicates would restate
the whole rule in a second language where the two could drift apart. A lender's loan
list is small enough that loading it and filtering is the cheaper mistake to avoid.

Loans feed the person page's `loan_balance` through `signed_balance`, so a loan you
took and an overpaid loan you gave both correctly reduce what a person owes you.

`totals_for_owner` reports the two directions **separately** rather than netted:
being owed 50,000 while owing 30,000 is a different situation from being owed
20,000, and one net figure cannot tell them apart. `net` is offered alongside for
anyone who wants the single number.

### Notes and reminders

Both attach to exactly one subject: a khata, a loan, or a person. The obvious model is
a generic `subject_type` + `subject_id` pair, and this deliberately is not that. A
generic pair cannot carry a foreign key, so nothing stops a note pointing at a khata
deleted last week, and every read has to branch on a string. Two nullable foreign keys
plus a person column, with a CHECK that exactly one is set, gets referential integrity
from the database and makes "notes on this loan" an ordinary indexed query. The cost is
one column per attachable kind; with three kinds that is cheaper than the integrity it
buys.

**A reminder has two dates.** `due_date` is when the money is expected; `remind_on` is
when the app should surface it. Collapsing them into one forces a choice between
nagging early and being told the day something is already late. A reminder whose
`remind_on` has not arrived is real but not yet anyone's problem, and reports
`is_surfaced: false` rather than being hidden outright.

`completed` maps to a timestamp rather than a boolean, so "when was this done" stays
answerable. Reminder status is derived from the calendar, like loan status.

A due date moved earlier can strand a `remind_on` that was valid when it was set, so
the pair is re-validated on every edit — and validated **before** anything is
assigned. Raising after mutating leaves the session holding a dirty object that a
later flush still tries to write. The same fault was found and fixed in the expense
service, where a rejected exact-split edit had been quietly changing the expense it
rejected.

### Timeline

`GET /people/{id}/timeline` merges six sources — group expenses, settlements, khata
entries, loans given, loan payments, and notes — into one history.

**Ordering is by calendar date first, timestamp second.** A khata entry dated last
Tuesday belongs on last Tuesday even though it was typed in today; the timestamp only
breaks ties within a day. Sorting purely by `created_at` produces a timeline that
reads as a data-entry log rather than a history.

Notes on a person's khata or loan appear on their timeline too: from a reader's point
of view all three are notes about the same relationship.

### Reports

`/reports/summary`, `/reports/khata`, `/reports/loans` and `/reports/activity` share
one shape: a date range, an optional person, and totals that add up. They read the
same rows every other page reads — nothing is precomputed or cached — so a report can
never describe a state the app is no longer in.

Loans given and taken are reported separately throughout — `loans_given` beside
`loans_taken`, `loan_payments` beside `loan_repayments_made`, `loans_receivable`
beside `loans_payable` — for the same reason the loan totals are. Borrowing counts
as money in and repaying it as money out, which is what `net_flow` reflects. The
activity chart follows loans you *gave* only: mixing in loans you took would invert
the meaning of both its series.

**Flows are windowed; positions are not.** `money_given`, `loan_payments` and the rest
are bounded by the date range. `khata_receivable`, `loans_receivable` and
`loans_payable` deliberately are not: what you are owed is a position, not a flow, and it does not reset because a
month ended. A report that windowed them would let a quiet month read as a settled
book, so both the schema and the UI say which is which.

The window defaults to the current month and is echoed back on every response, so a
report that is saved, printed or shared still says what it covers. A range that is
exactly one calendar month is labelled with that month's name.

`available_currencies` lists the currencies you have money actually recorded in, most
active first, and is **not** padded with your profile currency. A caller uses it to
decide whether the currency it was about to show has anything to show; padding it with
a guaranteed member would make that question unanswerable. Every currency stays
selectable regardless — this is only about picking a sensible default, so someone whose
khatas are all in rupees does not open Reports on a page of zeroes.

Activity series fill empty periods with zero rather than omitting them, so a chart gets
an even time axis instead of silently compressing the quiet stretches.

An adjustment is reported on whichever side its sign puts it: a correction to money
given or money received, not a third kind of movement.

### Production

```bash
docker compose up --build     # from the repository root
```

- Multi-stage build: wheels compiled in a builder image, so the runtime has no
  compiler and no `libpq-dev`.
- Runs as a non-root user, with a healthcheck on `/api/v1/health/live`.
- `docker-entrypoint.sh` waits for Postgres and runs `alembic upgrade head` before
  starting the server, so replicas cannot race a half-applied schema.
- `Settings` refuses to start when `ENVIRONMENT=production` and any of: the
  development `SECRET_KEY`, a secret under 32 characters, `DEBUG=true`, or
  `CORS_ORIGINS=*`. A placeholder signing key in production means anyone who has
  read the source can mint a token for any account, so that is a startup crash
  rather than a warning.
- Docs (`/docs`, `/redoc`, the OpenAPI JSON) are disabled in production.

### Indexes

Composite indexes cover the access patterns, not just individual columns:

| Index | Serves |
| --- | --- |
| `ix_expenses_group_date` | group expense lists and every group balance query |
| `ix_expenses_currency_date` | analytics slicing by currency and date |
| `ix_expense_splits_user_amount` | "my share of everything", the analytics hot path |
| `ix_group_members_user_group` | the visibility check on every scoped query |
| `ix_settlements_group_date` | a group's settlement history |
| `ix_settlements_pair` | settlements between two people |

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
