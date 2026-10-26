# QUOTATION {{quote_no}}

**From:** {{seller_company}}{% if seller_address %}, {{seller_address}}{% endif %}
**To:** {{customer_company}} — {{contact_name}}
**Your ref:** {{customer_rfq_ref}}
**Date:** {{date}}  |  **Valid until:** {{valid_until}}  |  **Currency:** {{currency}}

| Line | Tag | Description | Brand / Model | Qty | Unit price ({{currency_symbol}}) | Total ({{currency_symbol}}) | Lead time |
|---|---|---|---|---|---|---|---|
{{lines}}

**Subtotal:** {{currency_symbol}} {{subtotal}} (excl. VAT)
**Incoterms:** {{incoterms}}
**Payment terms:** {{payment_terms}}
**Documents included:** {{documents}}

**Notes / deviations:**
{{deviations}}

Prices exclude VAT unless stated. Subject to final confirmation of specifications and export compliance.

{{seller_name}}
{{seller_company}} | {{seller_email}} | {{seller_phone}}

{% if terms %}## Terms and conditions
{{terms}}{% endif %}
