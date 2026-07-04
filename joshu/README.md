# joshu/

Build + upstream-sync tooling for the optional [Joshu](https://github.com/db-aeon/joshu-oss)
cloud-desktop service (`docker-compose.joshu.yml`).

- `build.sh` — build the `joshu-oss` image from your local clone (`make joshu-build`)
- `sync.sh` — pull upstream changes, rebuild, restart (`make joshu-sync`)

Full documentation: [docs/joshu.md](../docs/joshu.md)
