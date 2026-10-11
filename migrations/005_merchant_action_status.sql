-- Authorize.net can hold an authorization for review (Fraud Detection Suite filters).
-- Hyperswitch reports that as requires_merchant_action: not failed, not authorized yet.
-- Found in sandbox testing: until a merchant approves or declines it in the
-- Authorize.net dashboard, the order must wait, not ask the customer to pay again.
ALTER TABLE payments DROP CONSTRAINT payments_status_check;
ALTER TABLE payments ADD CONSTRAINT payments_status_check CHECK (status IN (
    'creating', 'create_failed',
    'requires_payment_method', 'requires_confirmation', 'requires_customer_action',
    'processing', 'requires_merchant_action',
    'requires_capture', 'partially_captured', 'partially_captured_and_capturable',
    'succeeded', 'cancelled', 'failed'
));
