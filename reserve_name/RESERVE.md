# Reserving the name `runcomposer` (verified free 2026-07-06)

Two one-time owner actions — I can't do these without your account tokens.

## PyPI (the important one)

1. Create/log into your PyPI account, create an API token
   (https://pypi.org/manage/account/token/).
2. Then:

```bash
cd reserve_name/pypi
python -m pip install --upgrade build twine
python -m build
python -m twine upload dist/*        # paste the token (username: __token__)
```

## npm (secondary — grab it while it's free)

```bash
cd reserve_name/npm
npm login
npm publish
```

Notes:
- Both placeholders are honest "in development" 0.0.1 releases pointing at the
  GitHub repo — not empty squats (npm removes those).
- The GitHub side needs no reservation: `StochasticEntropy/runcomposer` is yours
  the moment you rename this repo (P0), and GitHub redirects the old name.
- After the first real release, delete this `reserve_name/` directory.
