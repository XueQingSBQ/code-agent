// user_service - Has a null pointer bug

class UserService {
    constructor(db) {
        this.db = db;
    }

    async getUser(id) {
        const user = await this.db.users.findById(id);

        // BUG: Doesn't check if user.email exists before accessing
        const normalizedEmail = user.email.toLowerCase();

        return {
            id: user.id,
            name: user.name,
            email: normalizedEmail,
        };
    }

    async updateUser(id, updates) {
        return await this.db.users.update(id, updates);
    }
}

module.exports = UserService;
