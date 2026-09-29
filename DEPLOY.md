# Deploying Model Forge on Railway

Everything here has been prepared and checked as far as it can be without a
Railway account. What is **not** verified is listed at the bottom, honestly,
so you know where to look if the first deploy stumbles.

---

## 1. How Blender gets installed, and why it is in the web image

This was the open question. The answer is a Dockerfile.

Railway's default builder, Nixpacks, reads `pyproject.toml` and builds a Python
app. It cannot install Blender. So the repo now has a `Dockerfile` that does,
and `railway.json` tells Railway to use it:

```dockerfile
RUN curl -fsSL "https://download.blender.org/release/Blender4.2/blender-4.2.5-linux-x64.tar.xz" \
      | tar -xJ -C /opt \
    && mv /opt/blender-4.2.5-linux-x64 /opt/blender \
    && ln -s /opt/blender/blender /usr/local/bin/blender
```

One file from blender.org, unpacked into `/opt/blender`. Not from apt, because
Debian's Blender is older and cannot be pinned to a version.

**Why the web image and not a separate worker.** A build currently runs inside
the HTTP request that starts it (`runner.build_now`), so the process serving
the page is the process that runs Blender. One image, one service. Moving
builds onto the job queue and a second service is the right end state - the
queue already exists, unused - but that is a code change, and this gets you
running first. The same image serves both roles when you do split them: the
start command decides.

**Three things in that Dockerfile that are not obvious:**

- **X11 and OpenGL libraries are installed even though no window ever opens.**
  Blender links against them at start-up in every mode. Without them
  `blender --background` fails with a loader error that reads like a corrupt
  download rather than a missing package.
- **The version is pinned exactly.** A Blender upgrade can change how an
  exporter names objects, and the names are the product here: every part has to
  stay separately selectable under the name the specification gave it.
- **51 MB of interface translations are deleted**, for a program that never
  shows an interface. Nothing else is trimmed: `scripts/addons` holds the FBX
  and glTF exporters and `python/lib` holds the numpy that glTF imports, so
  both are load bearing despite looking like fat.

**The build proves Blender works before it finishes.** The last step runs
`scripts/blender_smoke.py`, which builds a cube, exports it to all five
formats, renders a frame, and fails unless every file exists and is non-empty.
A broken image is then a failed build instead of a 500 on your first model.

That test is a real export on purpose. Asking
`hasattr(bpy.ops.export_scene, "fbx")` passes for operators that do not exist,
because `bpy.ops` fabricates attributes on demand, and checking `bpy.types`
only finds the Python addon exporters while OBJ and STL are built-in C
operators that never appear there. Both of those looked right and could not
fail. This one can, and I checked that it does.

Measured on a real Railway build: the pushed image is **613 MB** and the whole
build took **about 75 seconds**, of which Blender's download and unpack was 19
and pip was 18. Later builds reuse the Blender layer unless you change the
version.

(My first estimate here was 1.7 GB and five to ten minutes. That was the
uncompressed size of the Blender directory on disk and a guess at the download;
the layer compresses to a third of it and Railway's builders are fast.)

---

## 2. Services to create

**Two. Not three.**

| Service | What it is | Notes |
|---|---|---|
| **Postgres** | Railway's Postgres plugin | Provides `DATABASE_URL` |
| **Web** | This repo | Built from `Dockerfile` |

**A worker service is not needed yet, but one does no harm.** Builds run
inside the web request, so there is nothing for a worker to consume.

If a second service from this repo exists, it takes its role from its own
name: a service whose name contains `worker` or `build` runs the build worker,
anything else runs the web server. So a service called `worker` starts, reports
that it has nothing to poll, and idles. It does not need `DATABASE_URL`, it
does not migrate, and it does not crash-loop. Set `FORGE_ROLE=web` or
`FORGE_ROLE=worker` to override the guess.

This matters because both services print the same failure when misconfigured,
and two identical logs is how an afternoon disappears. The role and service
name are now the first line of every start-up:

```
==> forge: role=worker service=worker env=production
```

**A crashed worker never prevents the web service from working.** They are
separate containers. If the web service is up, the app is up.

Storage is not a service: it is an S3 bucket you already have, or a Railway
volume. See section 4.

---

## 3. Variables to set on the web service

**Set them in the environment that is actually deploying.** Railway keeps a
separate set of variables for every environment, and a project often has both
`production` and `staging`. Variables set in one do not exist in the other. The
environment selector is at the top of the project page.

This cost a whole afternoon here: four variables were set in `production` while
the deployment ran in `staging`, so the container saw none of them and every
symptom pointed at a bad database reference instead. The start-up log names the
environment for exactly this reason:

```
Running as service 'web' in environment 'staging' of project '3D Model Forge'
FORGE_* variables this container received: FORGE_BLENDER
```

`FORGE_BLENDER` comes from the Dockerfile, so a list containing only that one
means **nothing arrived from the platform at all**. That is an environment or
service mismatch, never a syntax problem.

A database reference only resolves to a service in the **same** environment. If
`Postgres` exists in `production` but the app runs in `staging`, add a database
to `staging` too, or paste the literal connection string.


**Required:**

| Variable | Value | Why |
|---|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` | Reference the Postgres service so it follows a password rotation |
| `FORGE_SECRET_KEY` | run `python run.py genkey` | Encrypts each workspace's API key at rest. **Losing it means every stored API key becomes unreadable** |
| `FORGE_SECURE_COOKIES` | `true` | Railway terminates TLS. Leave this off and the browser silently drops the session cookie, which looks exactly like sign-in failing for no reason |
| `FORGE_BASE_URL` | `https://${{ RAILWAY_PUBLIC_DOMAIN }}` | Used to build password reset links. Railway substitutes its own domain, so this needs no editing when the domain changes |

**Storage, one of the two options in section 4:**

| Variable | Value |
|---|---|
| `FORGE_S3_BUCKET` | your bucket name |
| `FORGE_S3_REGION` | e.g. `eu-west-2` |
| `FORGE_S3_ACCESS_KEY_ID` | key with `GetObject`, `PutObject`, `DeleteObject` on that bucket |
| `FORGE_S3_SECRET_ACCESS_KEY` | its secret |
| `FORGE_S3_PREFIX` | `forge` (optional; default `forge`) |
| `FORGE_S3_ENDPOINT` | only for a non-AWS S3: Cloudflare R2, Backblaze, MinIO |

**Optional, for password reset emails.** Without these the reset flow still
works, but the link goes to the service log instead of an inbox:

`FORGE_SMTP_HOST`, `FORGE_SMTP_PORT` (default 587), `FORGE_SMTP_USER`,
`FORGE_SMTP_PASSWORD`, `FORGE_MAIL_FROM`, `FORGE_SMTP_TLS`

`PORT` is set by Railway. Do not set it yourself.

---

## 4. Storage: this one will bite you if you skip it

**Railway wipes the container filesystem on every deploy.** With the default
local storage, every uploaded photo and every built model is deleted the next
time you push, while the database still lists them - so the app looks fine and
every download returns 404. Nothing in the interface can warn you, so the app
now says so in the log at start-up:

```
WARNING storage is LOCAL (/app/.storage). Fine for development. On Railway or
any container platform the filesystem is wiped on every deploy, so set
FORGE_S3_BUCKET, or attach a volume and point FORGE_LOCAL_STORAGE at it.
```

Pick one:

**S3 (recommended, and what you asked for).** Set the five `FORGE_S3_*`
variables. Downloads are then presigned URLs, so file bytes never stream back
through the app and the container's memory stays flat however large the uploads
get. At start-up the app writes and deletes one probe object and logs the
result, so a misspelled bucket or a key without `PutObject` shows up in the
deploy log rather than in somebody's first upload.

**A Railway volume (quicker to get going).** Attach a volume, mount it at
`/data`, set `FORGE_LOCAL_STORAGE=/data`. Survives deploys, but ties the
service to one machine and does not scale past one replica.

If uploads then fail with a permission error, that is the volume: the container
runs as uid 1000, not root, and a freshly mounted volume can be root-owned. The
fix is a one-off `chown 1000:1000 /data` from a shell on the service.

---

## 5. Deploy

```bash
# From the repo, on the branch you want deployed.
git push origin staging

# Then, in Railway: New Project -> Deploy from GitHub repo -> this repo.
# It will find Dockerfile and railway.json on its own.
```

`railway.json` already sets:

- **start command** `python run.py migrate && python run.py serve --host 0.0.0.0 --port $PORT`
- **health check** `/healthz`, which reports the database honestly rather than
  answering 200 while it is unreachable
- **replicas** 1

Migrations run on every boot and are safe to: they take a Postgres advisory
lock, so two replicas starting together cannot both apply the same migration
and fail the deploy on a schema that is actually fine.

---

## 6. When the service crashes a few seconds after starting

This is the most likely thing to go wrong, and it has one common cause.

This no longer crash-loops. The start command is `sh scripts/start.sh`, which
migrates, reports the outcome once, and starts the server either way. A
service with no database stays up, `/healthz` returns 503 so the platform still
marks the deployment unhealthy, and every page says the database is
unreachable. The earlier behaviour was `migrate && serve`, where the `&&` meant
a failed migration exited the container, the platform restarted it, and the
same page of guidance reprinted every five seconds until the log was
unreadable.

The connection string is read from whichever variable carries it:
`DATABASE_URL`, `POSTGRES_URL`, `POSTGRESQL_URL`, `DATABASE_PRIVATE_URL`,
`DATABASE_PUBLIC_URL`, or assembled from `PGHOST` with
`PGUSER`/`PGPASSWORD`/`PGDATABASE`/`PGPORT`. Referencing any of those works.

Open the deploy log and read the first few lines. The message now names the
variable and what to do about it:

- **`No database connection string found`** - the message lists every
  database-related variable the container can actually see. If that list says
  `NONE AT ALL`, nothing was shared with this service: add
  `DATABASE_URL=${{ Postgres.DATABASE_URL }}` to the **web service's** own
  Variables, using your Postgres service's exact name. Railway does not share
  a database with other services automatically, and setting the variable on
  the Postgres service, or in a variable group this service is not part of,
  has no effect here.
- **`could not be reached`** - the variable is set but the database is not
  answering. Check the Postgres service is Online, that the value is a
  reference rather than a URL pasted from another project, and that both
  services are in the **same** Railway project, or the private hostname will
  not resolve.

A missing `FORGE_SECRET_KEY` does **not** crash the app: it starts, and a
workspace simply cannot save an API key until the variable is set. So if the
service is crash-looping, the database is where to look.

---

## 7. First run

1. Open the app, **sign up**. The first account becomes the admin of its
   workspace.
2. **Settings** -> choose Claude or DeepSeek, paste an API key, **Save &
   verify**. Nothing is stored unless the key works.
3. **Projects** -> New project -> add an item -> upload a photo or a spec sheet.
4. **Read the files**, answer any dimensions it could not find, **Build 3D
   model**.

If the build fails, the job row keeps the reason and the service log has the
Blender output.

---

## 8. Sizing

A build peaks at **363 MB** resident, measured. With the web process alongside
it, **1 GB is comfortable and 512 MB is tight**, because a build shares the
container with the request serving it. One build takes 1 to 2 seconds for a
table, so CPU is not the constraint - concurrent builds are, and they are
serialised by nothing at the moment. That is the first reason you will want the
queue and a worker service.

---

## 9. What is not verified

Said plainly, because you deserve to know where the risk is:

- **The image build has been attempted once and fixed once.** The first
  Railway build failed in the smoke test because the script did not create its
  own output directory - locally it had always been given one that existed. The
  real build path was never affected (`runner.py` and `build.py` both create
  the directory), but the check meant to protect the build was the thing that
  broke it. Fixed, and the script now has an explicit exit code on every path,
  because Blender exits 0 when a script raises and an uncaught exception would
  have passed the build while proving nothing.

  Beyond that step the layers have not been assembled. If something else is
  wrong it will be a missing apt package, and the error will name it.

  Three messages in the build log are expected: the missing `locale` data
  (deleted deliberately), `EGL_BAD_MATCH` (no GPU, so Workbench falls back to
  software and still writes the PNG - the smoke test renders one and checks
  it), and "could not get a list of mounted file-systems".
- **No S3 bucket has been written to.** `S3Storage` satisfies its interface,
  constructs, and presigns correctly - and I fixed a real bug found while
  checking this: botocore was presigning with SigV2, which AWS rejects in every
  region created after 2014, so every download link would have returned 403.
  It now signs with SigV4, verified across four regions and an R2-style
  endpoint. What has not happened is a real `PutObject` against a real bucket;
  the start-up probe is there to tell you immediately if that fails.
- **No SMTP server has been used.** The reset flow is verified end to end with
  the link going to the log. The `smtplib` path is written but has not sent a
  message.
- **Builds are inline, not queued.** Fine for one person; two people building
  at the same time will contend. The queue exists and is unused.
