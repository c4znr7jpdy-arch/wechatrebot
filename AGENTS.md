# Repository instructions

## Remote update policy

- All customer upgrades after the initial installation must use the signed remote-update path already implemented in `src/wechatrebot_manager/updates.py`. Do not invent a second updater or distribute loose replacement files.
- Treat every released version as immutable. Any bug fix or feature update for customers must increment the semantic version. Never replace a package while keeping the same version number.
- Keep the version synchronized in `pyproject.toml`, `src/wechatrebot_manager/__init__.py`, and every numeric and string version field in `release/windows-version-info.txt`. Update customer-facing examples or preview fixtures when they name the current release.
- Before release, run the automated tests and `scripts/validate_release_metadata.py`, rebuild the frozen manager, and assemble a complete staging payload from the approved sources.
- A production update payload must have `source_manifest_status` set to `ready` by a genuinely approved `release/source-manifest.json`. Never bypass legal, redistribution, security, code-signing, clean-machine, or real-device gates merely to make the updater accept an audit build.
- Build and sign the update with `scripts/build_update_release.py` using `config/local/update-signing-private.pem`. The signing private key must remain off Git and must have an encrypted offline backup.
- The production package URL is under `https://ss.xiuxianjyj.xin/wechatrebot-updates/releases/`, and the signed channel manifest is `https://ss.xiuxianjyj.xin/wechatrebot-updates/stable.json`.
- Publish in this order: upload and verify the immutable versioned ZIP first, then atomically replace `stable.json` last. Never expose a manifest whose package is missing or only partially uploaded.
- Verify the live HTTPS ZIP size and SHA-256, the signed manifest, and an upgrade from the immediately previous installed release on a clean Windows machine before announcing completion.
- The customer console must check once shortly after startup and repeat according to the validated client update channel's `check_interval_seconds`; manual checks remain available. Automatic network failures must stay silent, while manual failures may be shown to the customer.
- Installation remains customer-confirmed and is only allowed while the robot is fully stopped. Do not turn this into an unannounced silent update.
- Rollbacks also require a higher semantic version containing the restored code. Do not point `stable.json` at an older version because installed clients intentionally ignore downgrades.
- Key rotation requires a transition release signed by the old key that embeds the new public key. Never replace the pinned public key and server key in one step.

The detailed operational runbook is in `docs/remote-updates.md`.
