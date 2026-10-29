// payment_app - Needs test coverage

class PaymentProcessor {
    constructor(gateway) {
        this.gateway = gateway;
    }

    async processPayment(amount, currency, paymentMethod) {
        // Validate amount
        if (amount <= 0) {
            throw new Error('Amount must be positive');
        }

        // Validate currency
        const supportedCurrencies = ['USD', 'EUR', 'GBP'];
        if (!supportedCurrencies.includes(currency)) {
            throw new Error('Unsupported currency');
        }

        // Process payment
        const result = await this.gateway.charge({
            amount,
            currency,
            source: paymentMethod,
        });

        if (result.success) {
            return {
                transactionId: result.id,
                status: 'completed',
                amount,
                currency,
            };
        } else {
            throw new Error(result.error || 'Payment failed');
        }
    }

    async refundPayment(transactionId, amount) {
        const result = await this.gateway.refund({
            transactionId,
            amount,
        });

        if (result.success) {
            return {
                refundId: result.id,
                status: 'refunded',
                amount,
            };
        } else {
            throw new Error(result.error || 'Refund failed');
        }
    }
}

module.exports = PaymentProcessor;
