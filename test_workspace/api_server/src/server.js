// api_server - Needs rate limiting

const express = require('express');
const app = express();

app.use(express.json());

// Login endpoint - NO RATE LIMITING (needs to be added)
app.post('/api/login', async (req, res) => {
    const { username, password } = req.body;

    // Authenticate user
    const user = await authenticateCredentials(username, password);

    if (user) {
        const token = generateToken(user);
        res.json({ token });
    } else {
        res.status(401).json({ error: 'Invalid credentials' });
    }
});

app.post('/api/register', async (req, res) => {
    const { username, email, password } = req.body;

    // Create user
    const user = await createUser({ username, email, password });

    res.json({ user });
});

async function authenticateCredentials(username, password) {
    // Stub
    return null;
}

function generateToken(user) {
    // Stub
    return 'token';
}

async function createUser(data) {
    // Stub
    return data;
}

module.exports = app;
