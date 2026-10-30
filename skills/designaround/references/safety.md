# Safety and conduct

## Legal wording

- Results are risk assessments. Say "the observer model rated it 3/10 similar" or "risk: low (uncalibrated)". Do not say "safe", "does not infringe", "you can sell this".
- Every filing package and risk report states that a registered practitioner must confirm.
- Do not draft responses to a court, a TRO or a platform complaint as if final. Prepare materials and recommend counsel; `gate create --type tro_response` asks a practitioner to review.

## Money

- Pass `--max-credits` on spending commands. If a run stops with `budget_exceeded`, report what was spent and ask before re-running with a higher cap.
- `billing checkout` only prints a URL. The user pays in the browser. Never try to automate payment.
- Managed packages: DesignAround collects only the platform fee. The practitioner's fee is paid directly to the practitioner.

## Keys and data

- Never pass API keys or provider keys as command arguments, never print them, never write them into files in the project.
- Case data stays on the user's machine. Screening uploads one product image and a text query; hosted observer calls upload one comparison image. Tell the user before the first upload if they have not used screening before.
- `evidence anchor` sends only a hash and a sequence number.

## Design integrity

- The user chooses the final design; record the choice with `case decide ... --person`.
- Do not rate your own candidates. Use `observe` so the score comes from an isolated model.
- Do not copy a competitor's design into a new case as "original" to file it. If the user asks to file someone else's design, refuse and explain that the applicant must be the creator or have rights.
- Keep `locked_params` unless the user agrees to change them.

## Agent runtime isolation

The embedded agent team (platform/byok modes) uses its own Pi Agent configuration in `~/.designaround/pi-agent`. It never reads `~/.pi`, has no shell or file tools, and can only call DesignAround CAD tools. `designaround agent check` verifies this.
