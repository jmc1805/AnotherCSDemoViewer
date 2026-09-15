# Vendored fonts

Downloaded by `tools/fetch_fonts.py` from the Google Fonts API and committed so
the app renders identically with no network - see `static/fonts.css`.

| Family | Licence | Upstream |
|---|---|---|
| Archivo | SIL Open Font License 1.1 | https://github.com/Omnibus-Type/Archivo |
| Archivo Narrow | SIL Open Font License 1.1 | https://github.com/Omnibus-Type/ArchivoNarrow |
| Roboto Mono | SIL Open Font License 1.1 | https://github.com/googlefonts/robotomono |

The OFL permits bundling and redistribution with an application, including
commercially, provided the fonts are not sold on their own and the licence
travels with them. That is why these are committed while `static/assets/` and
`static/map/` - Valve game content - are gitignored.
