# Food Appears

Bar widget (`jordan.grocy`) for [Omarchy](https://omarchy.org) 4.x. It shows what is in stock on your Grocy server and the shopping list for the following Monday. When you configure Kroger, it can also search one banner store, show regular and promo prices, and add a priced hit to that list.

## Requirements

- Omarchy 4.x, with the `omarchy` command. The widget runs inside the Omarchy shell.
- `/usr/bin/python3`. The bar widget calls that path with `-I` and `-S` and a cleared environment, so the helper uses the Python standard library. GitHub Actions runs the tests on Python 3.12.
- The CA bundle at `/etc/ssl/certs/ca-certificates.crt`. The widget sets `SSL_CERT_FILE` to that path.
- A Grocy server. `GROCY_URL` is an `https` URL with a hostname and an optional port. The helper accepts a name that resolves to a public address, a private LAN address, or a Tailscale address (`100.64.0.0/10` or `fd7a:115c:a1e0::/48`). It refuses a raw IP, a user, a password, a path, a query, a redirect, loopback, link-local, and other reserved addresses. The resolved address is pinned for the request.
- Optional: a Kroger developer app from <https://developer.kroger.com/> with the `product.compact` scope, and the 8-digit id of the store you shop.

## Install

`omarchy plugin add` asks you to confirm the repository URL, clones the default branch into `~/.config/omarchy/plugins/jordan.grocy`, checks `manifest.json`, and with `--enable` turns the widget on. Enabling a bar widget places it in `barWidget.defaultSection`. This manifest sets that to `right`.

```bash
omarchy plugin add https://github.com/jordanpartridge/omarchy-grocy.git --enable
```

To put it on the right again later:

```bash
omarchy bar move jordan.grocy --section right
```

Updates show the diff, then fast-forward the checkout:

```bash
omarchy plugin update jordan.grocy
```

A checkout of tag `v0.4.0` cannot fast-forward onto the default branch. The tag and the default branch are two different commits on top of the initial commit. Remove the plugin and run the add command again.

## Usage

Click the bar button. Its tooltip is "Food Appears".

The popup loads stock and the order. The list name is `Order YYYY-Www` for the ISO week of the following Monday in `GROCY_TIMEZONE`. On Sunday the coming Monday's order is already placed, so new adds use the Monday after it. The same name is reused. A list is created only when that name is missing.

Stock search filters the rows already loaded. Choosing a row adds that product, amount 1, to the order. Take off removes that product's lines from the named list.

Store search stays hidden until the Kroger keys are set. A search needs at least 3 characters. The helper asks the saved store for a page of 24 hits and shows each name, the regular price, and the promo when it is above zero. Otherwise the row says no sale. A row with no regular price says no price, and Add is dimmed. More loads the next page until 72 hits are on screen. Add works for a hit that has a name and a regular price, and it writes amount 1. Change asks for a 5-digit ZIP, lists up to 8 stores of `KROGER_CHAIN` near it, and Use saves that store after Kroger confirms the banner. The stepper on a store line sets the amount from 1 to 99. Stock lines show an amount and have no stepper.

A store line on the Grocy list keeps the Kroger product id and the UPC when Kroger sent one:

`store|{price}|{promo or -}|{productId}|{upc or -}|{name}`

Kroger calls stay on `https://api.kroger.com`, which must resolve to a public address. The access token is cached at `~/.local/state/jordan-grocy/kroger.token` (mode `600`) and is sent as a Bearer header to that host. The helper leaves the Kroger cart alone.

`~/.local/state/jordan-grocy/add.lock` keeps two adds, or an add and a take-off, from creating two lists. The lock holds no stock and no key.

### Grocy

- `GET /api/stock`
- `GET /api/objects/shopping_lists`
- `POST /api/objects/shopping_lists` when the next-week list is missing
- `DELETE /api/objects/shopping_lists/{id}` only for a list this plugin just created and then found was not the oldest list of that name, or for a list this plugin just created when adding a store hit failed before the line was saved
- `POST /api/stock/shoppinglist/add-product` with `product_id`, `list_id`, and `product_amount` 1
- `POST /api/objects/shopping_list` with `shopping_list_id`, `amount`, and the store note above
- `PUT /api/objects/shopping_list/{id}` with `amount` when the quantity stepper changes a store line
- `GET /api/objects/shopping_list` and, when needed, `GET /api/objects/products`
- `DELETE /api/objects/shopping_list/{id}` to take a line off

### Kroger

Used when store search is configured:

- `POST /v1/connect/oauth2/token` with grant `client_credentials` and scope `product.compact`
- `GET /v1/locations/{id}` and `GET /v1/locations` with `filter.zipCode.near`, `filter.chain`, and `filter.limit` 8
- `GET /v1/products` and `GET /v1/products/{id}` with `filter.locationId`

## Config

The helper reads one file. It resolves your home directory with the password database (`pwd.getpwuid`) and opens `.config/jordan-grocy/config.env` there. `XDG_CONFIG_HOME` is unused. Use mode `600`. The helper rejects group or other permission bits, a second hard link, and a symlink. The directory must be free of group and other write bits.

```bash
mkdir -p "$HOME/.config/jordan-grocy"
cp config.example "$HOME/.config/jordan-grocy/config.env"
chmod 700 "$HOME/.config/jordan-grocy"
chmod 600 "$HOME/.config/jordan-grocy/config.env"
```

Replace `https://grocy.example` with your server and set `GROCY_API_KEY`. The key is 32 to 128 hex characters. It is sent only as a `GROCY-API-KEY` header. `config.example` is a template, and `https://grocy.example` is only there to show the URL shape.

| Key | Required | Purpose |
| --- | --- | --- |
| `GROCY_URL` | yes | `https` URL of your Grocy server. Hostname, optional port. |
| `GROCY_API_KEY` | yes | API key from Grocy, 32 to 128 hex characters. |
| `GROCY_TIMEZONE` | no | IANA timezone for the Monday list name. Defaults to `UTC`. |
| `KROGER_CLIENT_ID` | no | Kroger developer app id. Leave the Kroger keys unset to hide store search. |
| `KROGER_CLIENT_SECRET` | no | Kroger app secret. Sent as HTTP Basic to `https://api.kroger.com` when asking for a token. |
| `KROGER_LOCATION_ID` | no | 8-digit id of the store you shop. Change in the popup rewrites only this line. |
| `KROGER_CHAIN` | no | Banner code for `filter.chain`, such as `FRYS` or `SMITHS`. Defaults to `FRYS` when the line is absent. |

State files, including the token cache and `add.lock`, live under `.local/state/jordan-grocy` in that same home directory. `XDG_STATE_HOME` is unused.

## Uninstall

```bash
omarchy plugin remove jordan.grocy
```

That disables the plugin and deletes the git checkout. The config file and the state directory stay. Remove them when you want them gone:

```bash
rm -rf "$HOME/.config/jordan-grocy" "$HOME/.local/state/jordan-grocy"
```

## Development

GitHub Actions (`.github/workflows/ci.yml`) installs these Ubuntu packages, then runs the checks:

```bash
sudo apt-get update && sudo apt-get install -y \
  g++ pkg-config \
  qt6-base-dev qt6-declarative-dev \
  qt6-declarative-dev-tools \
  qml6-module-qtquick qml6-module-qtqml \
  qml6-module-qtqml-workerscript
python3 -m pip install ruff
ruff check .
python3 scripts/check_manifest.py
python3 scripts/scrub_check.py
QT_QPA_PLATFORM=offscreen python3 tests/test_grocy.py
```

The secret scan in CI downloads gitleaks 8.28.0 and runs:

```bash
gitleaks detect --source . --redact --no-banner
```

The tests talk to a loopback stand-in for Grocy and a loopback stand-in for Kroger. Loopback is allowed only inside the test process. The tests build their own config in that process.

## Release

### v0.4.0

`manifest.json` says `0.4.0`. The git tag `v0.4.0` points at `0eb914cfa1f3e77103bab4b9d3f6ef5dd5a8a970`. The Releases page for this repository is empty.

The tag ships this widget: Grocy stock, the shopping list named `Order YYYY-Www` for the following Monday, and optional Kroger search for one banner store with regular price, promo price, and add-to-list. The helper reads `config.env` from the password-database home. Kroger traffic is the token, location, and product calls listed above.

The Actions run on the tag push failed `tests/test_grocy.py` in `ModelDecisionTest`. `qmltime` exits 255 after the QML file calls `Qt.exit`. Commit `958e55d` on the default branch accepts that exit when the script printed `search-exit-ok`. At that commit, `BarWidget.qml`, `bin/grocy.py`, `Model.js`, `manifest.json`, and `config.example` match the tag. A store row with no regular price shows U+2014 on the tag. The default branch shows the words "no price" there.

`omarchy plugin add` clones the default branch. That is the branch whose test file accepts the `qmltime` exit.

## License

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 Jordan Partridge.
