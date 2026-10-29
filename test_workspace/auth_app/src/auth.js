// auth_app - Authentication module to refactor

function authenticateUser(token) {
    // Parse token
    const parts = token.split('.');
    if (parts.length !== 3) {
        return { valid: false, error: 'Invalid token format' };
    }

    // Validate token (inline - should be extracted)
    const header = JSON.parse(atob(parts[0]));
    const payload = JSON.parse(atob(parts[1]));
    const signature = parts[2];

    const now = Date.now() / 1000;
    if (payload.exp && payload.exp < now) {
        return { valid: false, error: 'Token expired' };
    }

    if (payload.iat && payload.iat > now) {
        return { valid: false, error: 'Token not yet valid' };
    }

    // Verify signature (simplified)
    const expectedSignature = require('crypto')
        .createHmac('sha256', process.env.JWT_SECRET)
        .update(parts[0] + '.' + parts[1])
        .digest('base64url');

    if (signature !== expectedSignature) {
        return { valid: false, error: 'Invalid signature' };
    }

    return { valid: true, user: payload };
}

module.exports = { authenticateUser };
