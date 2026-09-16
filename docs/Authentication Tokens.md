# Authentication Tokens — My Understanding

The authentication system uses **two different tokens**:

1. **Access Token**
2. **Refresh Token**

They have different jobs.

---

## 1. Access Token

The Access Token is the token that I use when accessing protected APIs.

For example:

```text
GET /account/me
Authorization: Bearer <access_token>
```

The Access Token should be **short-lived**.

For example:

```text
ACCESS_TOKEN_EXPIRE_MINUTES=10
```

So if I log in at:

```text
Monday 10:00 AM
```

my Access Token might expire at:

```text
Monday 10:10 AM
```

The important thing is:

> The Access Token does NOT need to live for 7 days.

It is only meant to give the user temporary access to the API.

### Why make it short-lived?

Because if someone steals the Access Token, they can use it until it expires.

For example:

```text
Access Token stolen
       ↓
Attacker can use it
       ↓
Token expires after 10 minutes
       ↓
Attacker can no longer use it
```

So a short Access Token limits the damage.

---

# 2. Refresh Token

The Refresh Token has a different purpose.

It is used to get a **new Access Token** after the old Access Token expires.

The user should not have to log in again every 10 minutes.

For example:

```text
Login
  ↓
Access Token = 10 minutes
Refresh Token = 7 days
```

After 10 minutes:

```text
Access Token expires
       ↓
Client sends Refresh Token
       ↓
Server checks Refresh Token
       ↓
Server creates NEW Access Token
```

The user stays logged in.

---

# 3. Where are the tokens stored?

The Access Token is normally returned to the client and kept in the client's memory/state.

The Refresh Token is stored in an:

```text
HttpOnly Cookie
```

The important difference is:

```text
Access Token
→ used frequently
→ short lifetime
→ stateless

Refresh Token
→ used to get new Access Tokens
→ longer lifetime
→ associated with a server-side session
```

The Refresh Token session is stored in Redis.

For example:

```text
session:user_id:random_session_id
```

Redis also gives this session a TTL.

---

# 4. The 7-Day Inactivity Rule

The important thing I need to understand is:

> The Refresh Token does NOT necessarily mean "the user gets exactly 7 days from login."

Instead, I can make it a **sliding/rolling session**.

The rule becomes:

> If the user is active, extend the session another 7 days.

> If the user is inactive for 7 days, the session expires.

---

# 5. Example

Suppose I log in on Monday.

The server creates:

```text
Refresh Token
+
Redis session
+
TTL = 7 days
```

So initially:

```text
Monday
  ↓
7-day timer starts
  ↓
Expires next Monday
```

Now imagine the user is active on Friday.

Their Access Token has expired, so the client calls:

```text
POST /refresh
```

The server verifies the Refresh Token and finds the Redis session.

Because the user is active, I reset the Redis TTL:

```python
session_ttl = REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60

await redis.expire(session_key, session_ttl)
```

Now the timer starts again from Friday.

Instead of:

```text
Monday → next Monday
```

it becomes:

```text
Friday → next Friday
```

So the user doesn't have to log in again.

---

# 6. What happens if the user stops using the application?

Suppose the user last used the application on Friday.

Then they disappear for 8 days.

The Redis session has:

```text
TTL = 7 days
```

Eventually:

```text
TTL → 0
```

Redis automatically removes the session.

Now the user comes back on the 8th day.

The client sends the Refresh Token:

```text
POST /refresh
```

The server checks Redis:

```text
session exists?
```

Redis says:

```text
No
```

So the server rejects the Refresh Token.

The user must log in again.

Therefore:

```text
Active user
    ↓
Refresh
    ↓
7-day timer resets
    ↓
User stays logged in


Inactive user
    ↓
7 days pass
    ↓
Redis session disappears
    ↓
Refresh fails
    ↓
Login required
```

This is what **sliding session / rolling refresh expiration** means.

---

# 7. Why `/account/me` can still work after logout

This part confused me initially.

Suppose I have:

```text
Access Token
Refresh Token
```

I log out.

The logout endpoint deletes the Redis session and the Refresh Token cookie.

But the Access Token might still be valid.

Why?

Because my Access Token is a JWT.

The server can verify the JWT using its signature and expiration time.

For example:

```text
Access Token
expires in 10 minutes
```

When I logout after 2 minutes:

```text
Logout
 ↓
Refresh session deleted
 ↓
Refresh cookie deleted
 ↓
BUT
 ↓
Access Token still has 8 minutes left
```

If `/account/me` only checks whether the JWT is valid, the server can still accept it.

So:

> Deleting the Refresh Token does NOT automatically destroy an already-issued Access Token.

This is expected if Access Tokens are being handled as stateless JWTs.

---

# 8. Why deleting the wrong cookie is a bug

My Refresh Token is stored in:

```text
refresh_token
```

So during logout I need to delete:

```python
response.delete_cookie(key="refresh_token")
```

I should NOT do:

```python
response.delete_cookie(key="access_token")
```

because the Access Token was not stored in that cookie.

It was returned in the response body and is being sent later as:

```text
Authorization: Bearer <access_token>
```

Therefore:

```text
delete access_token cookie
        ↓
does nothing
```

because there is no such cookie.

The correct logout operation is:

```text
1. Delete Redis refresh session
2. Delete refresh_token cookie
3. Client removes its Access Token
```

---

# 9. Two different things happen during logout

This is important.

### Server side

The server should destroy the Refresh Token session:

```text
Redis

session:user:abc123
        ↓
       DELETE
```

And remove the browser cookie:

```text
refresh_token
        ↓
      DELETE
```

### Client side

The frontend should also remove the Access Token from its memory/state.

```text
Access Token
     ↓
remove it
```

Then the frontend won't send it anymore.

---

# 10. What if I want the Access Token to be killed immediately?

There are two approaches.

## Approach A — Short-lived Access Token

Use a short expiration:

```text
Access Token = 5–10 minutes
```

When the user logs out:

```text
Refresh session → deleted
Refresh cookie → deleted
Access token → client removes it
```

If someone somehow still has the Access Token, it will naturally expire soon.

This keeps the authentication system relatively simple.

---

## Approach B — Access Token Blacklist

If I want the server to immediately reject the Access Token after logout, I can store a blacklist entry in Redis.

For example:

```text
blacklist:<token>
```

Then `get_current_user()` checks:

```text
Is this Access Token blacklisted?
       ↓
    Yes → 401
    No  → continue
```

The blacklist entry only needs to exist until the Access Token itself expires.

For example:

```text
Access Token has 4 minutes remaining

blacklist token
TTL = 4 minutes
```

After 4 minutes, Redis automatically removes the blacklist entry.

---

# 11. The simple architecture I should remember

The easiest way for me to remember the whole system is:

```text
                     LOGIN
                       │
             ┌─────────┴─────────┐
             ↓                   ↓
       Access Token         Refresh Token
       5–10 minutes             7 days
             │                   │
             │                   ↓
             │                 Cookie
             │                   │
             │                   ↓
             │                 Redis
             │                   │
             ↓                   ↓
      Protected APIs       Create new Access
                              Token
```

Then:

```text
Access Token expires
        ↓
POST /refresh
        ↓
Refresh Token checked
        ↓
Redis session checked
        ↓
Valid?
   ↓         ↓
  Yes        No
   ↓          ↓
New token    Login again
   ↓
Reset 7-day Redis TTL
```

---

# 12. The most important concept

I should not think:

> "The Refresh Token lasts 7 days."

I should think:

> **"The user can remain logged in as long as their Refresh Token session is continuously active. If they are inactive for 7 days, the session expires."**

So the timeline looks like this:

```text
LOGIN
  │
  ▼
Monday
Refresh session = 7 days
  │
  │ user active
  ▼
Friday
Refresh session = reset to 7 days
  │
  │ user active
  ▼
Wednesday
Refresh session = reset to 7 days
  │
  │ user disappears
  ▼
7 days later
Redis session expires
  │
  ▼
User must login again
```

That's the whole idea.

---

# 13. My final mental model

**Access Token = temporary key**

```text
Short life
Used for API requests
Can expire frequently
Should not need Redis for every request
```

**Refresh Token = way to get another temporary key**

```text
Longer life
Stored in HttpOnly cookie
Connected to Redis session
Can be invalidated server-side
Its inactivity timer can be reset
```

**Redis session = server's record saying "this login session is still alive."**

```text
Access Token
     ↓
"Can I access the API?"

Refresh Token
     ↓
"Can I get another Access Token?"

Redis session
     ↓
"Is this login session still allowed?"
```

So the three pieces have **three different jobs**. That separation is what makes the authentication system easier to reason about and secure.
