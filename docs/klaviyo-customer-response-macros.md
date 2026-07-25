# Klaviyo customer response macros

Last reviewed: 2026-07-23

## Purpose

These macros are reusable human replies for Klaviyo Helpdesk. They complement
the automated lifecycle flows; they are not flow emails and must never be sent
without an agent reviewing the customer context first.

All replies use the same personal signature:

```text
Best,
Jean-Ludovic
Founder, Toorow
```

Use the Helpdesk profile variable `{{first_name}}`. Klaviyo inserts a blank
value when the profile has no first name, so the agent must always review the
preview and replace the greeting with `Hi there,` when necessary.

## Macro library

### 1. General acknowledgement

- **Internal title:** `EN — General — Acknowledgement`
- **Category:** `General`
- **Initial status:** `Draft`

```text
Hi {{first_name}},

Thanks for reaching out. I’ve received your message and I’m reviewing the details now.

I’ll get back to you with a clear answer or the next step as soon as possible.

Best,
Jean-Ludovic
Founder, Toorow
```

### 2. Connector request received

- **Internal title:** `EN — Waitlist — Connector request`
- **Category:** `Early access`
- **Initial status:** `Draft`

```text
Hi {{first_name}},

Thanks for telling us which connector you need first. I’ve added your request to our early-access priorities.

If you can, reply with the reporting tool or workflow you want to connect it to. That context helps us validate the most useful setup.

Best,
Jean-Ludovic
Founder, Toorow
```

### 3. New invitation requested

- **Internal title:** `EN — Access — Renew invitation`
- **Category:** `Access`
- **Initial status:** `Draft`

```text
Hi {{first_name}},

Thanks for letting me know. I’m checking your access and will send a new secure invitation if the previous one has expired.

For security, please use only the most recent invitation link you receive from Toorow.

Best,
Jean-Ludovic
Founder, Toorow
```

### 4. Connector setup help

- **Internal title:** `EN — Setup — Connector help`
- **Category:** `Setup`
- **Initial status:** `Draft`

```text
Hi {{first_name}},

I’m happy to help with the connection.

Please reply with:
- the connector name;
- the step where the setup stops;
- the exact error message, if one is shown;
- whether the source account is visible in Toorow.

Please do not send passwords, API keys, access tokens, or other credentials.

Best,
Jean-Ludovic
Founder, Toorow
```

### 5. Data quality investigation

- **Internal title:** `EN — Data — Investigation opened`
- **Category:** `Data quality`
- **Initial status:** `Draft`

```text
Hi {{first_name}},

Thanks for flagging this. I’m reviewing the affected datastream, its latest synchronization, and the metric definition before drawing a conclusion.

Please send the metric name, date range, expected value, observed value, and the report or source you are comparing against. A screenshot is helpful, but please remove any sensitive customer data.

I’ll come back with the evidence I can verify and any remaining uncertainty.

Best,
Jean-Ludovic
Founder, Toorow
```

### 6. Full plan request

- **Internal title:** `EN — Plan — Full plan request`
- **Category:** `Plan`
- **Initial status:** `Draft`

```text
Hi {{first_name}},

Thanks for your interest in the Toorow Full plan.

I’m reviewing your organization’s current setup and the additional datastream or backfill capacity you need. I’ll confirm the appropriate next step before any plan change is made.

Best,
Jean-Ludovic
Founder, Toorow
```

### 7. Resolution and confirmation

- **Internal title:** `EN — General — Resolution follow-up`
- **Category:** `General`
- **Initial status:** `Draft`

```text
Hi {{first_name}},

The issue has now been addressed on our side.

Could you try the same action again and confirm whether everything now works as expected? If the problem continues, reply with the time of your latest attempt and the exact message shown.

Best,
Jean-Ludovic
Founder, Toorow
```

## Klaviyo installation

The current Klaviyo account does not have Helpdesk enabled. Do not start a
Helpdesk trial solely to install these macros.

When Helpdesk is intentionally enabled:

1. Open **Service > Helpdesk > Settings > Macros**.
2. Create each macro using the internal title, category, and body above.
3. Keep every macro in **Draft** until its preview has been checked with a
   profile that has a first name and one that does not.
4. Confirm that replies are sent from the support channel and that the human
   agent can edit the message before sending.
5. Activate macros individually after a real ticket has been used to verify
   formatting and personalization.

## Writing rules

- Be direct, calm, and specific.
- Never claim that an issue is resolved before verification.
- Never request credentials or secrets.
- Separate verified facts from assumptions.
- Remove irrelevant paragraphs before sending.
- Keep the personal signature on human replies only; automated lifecycle
  emails continue to use `The Toorow team`.
