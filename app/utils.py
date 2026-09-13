from pwdlib import PasswordHash

# Initialize PasswordHash using pwdlib recommended hasher (Argon2id with bcrypt fallback)
password_hash = PasswordHash.recommended()


def hash(password: str) -> str:
    """
    Hashes a plaintext password using a secure algorithm (Argon2id).

    Args:
        password: The plain text password to hash.

    Returns:
        The securely hashed password string.
    """
    return password_hash.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verifies a plaintext password against a stored hashed password.

    Args:
        plain_password: The plaintext password entered by the user.
        hashed_password: The previously hashed password from the database.

    Returns:
        True if password matches the hash, False otherwise.
    """
    return password_hash.verify(plain_password, hashed_password)