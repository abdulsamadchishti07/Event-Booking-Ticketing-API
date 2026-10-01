# Sweeper

### Definition

A **sweeper** is a background process that periodically checks the database/system and **cleans up expired, stale, or unwanted data**.

> **Sweeper = Janitor of the database**

### Why is it used?

- Removes expired data
- Cleans old/stale records
- Handles stuck or abandoned jobs
- Keeps the database clean
- Prevents unnecessary data buildup

### How it works

```text
Cron / Scheduler
       ↓
   Sweeper
       ↓
 Scan Database
       ↓
Find stale/expired data
       ↓
Delete / Update / Fix
```

### Example

Expired sessions:

```text
Session expires
      ↓
Sweeper checks
      ↓
Expired?
      ↓
Delete / Invalidate
```

### Sweeper vs Cron

- **Cron:** decides **WHEN** to run.
- **Sweeper:** decides **WHAT** to clean.

### Sweeper vs Worker

- **Worker:** processes a specific background job.
- **Sweeper:** searches for things that **need maintenance**.

### One-line definition

> A **sweeper** is a scheduled background process that finds and cleans expired, stale, or inconsistent data.

### Example

Suppose users have **login sessions** that expire after 24 hours.

- Sweeper runs every 1 hour.
- Checks the database for expired sessions.
- Finds expired sessions.
- Deletes or invalidates them.

**Flow:**
`Scheduler → Sweeper → Check DB → Find expired data → Clean it`

**Example:**
`Expired Session → Sweeper → Delete Session`
