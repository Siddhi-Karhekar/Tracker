# Incident response

What to do when something goes wrong. Every step uses something that already exists in TeamPulse. Write down what
happened and when; the activity log helps.

## An API key leaked (committed, pasted in chat, screenshotted)

1. **Revoke it at the provider first** (GitHub settings → tokens, Notion integrations, Discord developer portal,
   Groq / Gemini console). Removing it from the repo doesn't help: it's already copied.
2. Make a new key and put it in `.env` only.
3. If it was committed, the history still has it. Run `python scripts/scan_secrets.py`: it reports every file and commit that contains it (masked).
   Rewriting history is optional once the key is revoked.
4. Check the provider's logs for use you don't recognise.

## An account may be compromised

1. A leader opens **Team → Accounts** and resets the person's password. This signs them out everywhere, clears
   two-factor so they can set it up again, and needs the leader's own recent confirmation.
   Without the app: `python -m teampulse.auth reset <username>`.
2. The reset also revoked their AI assistant tokens; check Team → AI assistant connections shows none active.
3. Read the activity log filtered by that person for the time since the last trusted sign-in.
   Look for `auth.session_moved`, failed `auth.login_code` entries and changes they wouldn't make.
4. If a leader account was compromised, also check the Capabilities tab for grants that shouldn't be there.

## The activity log check fails

The Security page shows "Activity log intact: no" when an entry was changed or removed outside the app.

1. Don't fix it. Copy the database file (`TEAMPULSE_DB`) as it is, for evidence.
2. The page names the first entry that doesn't match. Everything before it is trustworthy.
3. Whoever can write the database file can do this, so it means someone had access to the machine. Treat it like
   the account compromise above, for everyone with access to that machine.
4. Restore from the newest backup made before that entry if the data itself was changed.

## A contribution approval lapsed

This means evidence the member approved was changed or removed afterwards. The member is told, and the report goes
back to waiting for review. The Contributions page says which items changed. Ask the person who changed them why.

## The AI said something harmful or wrong

1. AI answers are suggestions; nothing was changed by it. Correct it in the team chat.
2. If it was caused by injected text, the Security page → AI safety lists the source item. Delete or edit it at the
   source.
3. Add the case to the evaluation dataset so it's tested from now on.

## Restoring a backup

```
python -m teampulse.maintenance verify  <backup file>
python -m teampulse.maintenance restore <backup file> --yes
```

Stop the app, the Discord bot and the poll loop first; `--yes` confirms they're stopped.

Encrypted backups need `TEAMPULSE_BACKUP_KEY`. Without the key they can't be read, by anyone.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md#reporting-a-problem).
