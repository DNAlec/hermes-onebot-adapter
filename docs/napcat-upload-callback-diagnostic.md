# NapCat Group Upload Callback Diagnostic

This procedure distinguishes two failure modes in NapCat `v4.18.13`:

1. QQNT never emits a final successful `onMsgInfoListUpdate` record.
2. QQNT emits one, but its `guildId` or `sendStatus` does not satisfy NapCat's correlation predicate.

The diagnostic patch only adds warning logs. It does not change upload behavior or success criteria. The logs contain
QQ/group identifiers, message IDs, filenames, file UUIDs, and sizes; treat them as sensitive and restore the original
bundle immediately after the controlled reproduction.

## Prepare

Build from the exact deployed tag so unrelated NapCat changes do not affect the result:

```bash
git clone --branch v4.18.13 --depth 1 https://github.com/NapNeko/NapCatQQ.git /tmp/NapCatQQ-v4.18.13
git -C /tmp/NapCatQQ-v4.18.13 apply \
  /home/alec/workspace/hermes-onebot-adapter/scripts/napcat-v4.18.13-upload-callback-diagnostic.patch
cd /tmp/NapCatQQ-v4.18.13
corepack pnpm install --frozen-lockfile
corepack pnpm build:webui
NAPCAT_VERSION=4.18.13 corepack pnpm build:shell
```

With pnpm 11, run `corepack pnpm approve-builds --all` and repeat installation if pnpm initially reports
`ERR_PNPM_IGNORED_BUILDS` in this disposable source checkout.

The diagnostic bundle is expected at:

```text
/tmp/NapCatQQ-v4.18.13/packages/napcat-shell/dist/napcat.mjs
```

Before deployment, confirm that the bundle contains `UploadCallbackDiag`.

The bundle prepared during the initial investigation is currently at:

```text
/tmp/opencode/NapCatQQ-v4.18.13-check/packages/napcat-shell/dist/napcat.mjs
SHA-256: 2b5f0d3fd301137bf1da66c2ec23fcdfa7890b0acc9b9b1189435302b96bd1a9
```

## Deploy Later

Deployment requires a brief NapCat restart. Do not run these commands during preparation:

```bash
docker cp napcat:/app/napcat/napcat.mjs /tmp/napcat.mjs.v4.18.13.backup
docker cp /tmp/NapCatQQ-v4.18.13/packages/napcat-shell/dist/napcat.mjs napcat:/app/napcat/napcat.mjs
docker restart napcat
```

The replacement survives `docker restart`, but not container recreation or image upgrade.

## Reproduce

Upload one uniquely named small file through `onebot_upload_file`. Avoid concurrent file uploads during the test.
Use a quiet test window because the patch records compact metadata for every `onMsgInfoListUpdate` callback while the
diagnostic upload is pending.

Capture only diagnostic lines:

```bash
docker logs --since 10m napcat 2>&1 | rg 'UploadCallbackDiag'
```

Interpretation:

- `start` and `service`, but no `update`: QQNT emitted no relevant file update.
- `update` exists, but `guildId` differs from `expectedCorrelation`: NapCat's correlation key was lost or replaced.
- `guildId` matches, but `sendStatus` never reaches the success value: QQNT never reported final success.
- `matched` appears promptly, but the OneBot response is still delayed: investigate the WebSocket action response path instead.

Keep the complete diagnostic lines, the upload start time, group ID, requested filename, resulting group message sequence, and whether the file actually appeared.

## Restore

```bash
docker cp /tmp/napcat.mjs.v4.18.13.backup napcat:/app/napcat/napcat.mjs
docker restart napcat
```

Remove `/tmp/NapCatQQ-v4.18.13` after the diagnosis is complete.
