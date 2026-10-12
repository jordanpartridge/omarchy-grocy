.pragma library

function filterStock(rows, query) {
  var q = String(query || "").toLowerCase()
  var out = []
  var list = rows || []
  var limit = Math.min(list.length, 400)
  for (var i = 0; i < limit; i++) {
    var row = list[i]
    if (!row || typeof row.name !== "string") continue
    if (q && row.name.toLowerCase().indexOf(q) === -1) continue
    out.push(row)
    if (out.length >= 400) break
  }
  return out
}

function money(text) {
  var value = String(text || "")
  if (!/^\d{1,4}\.\d{2}$/.test(value)) return false
  var number = Number(value)
  return isFinite(number) && number > 0 && number <= 9999.99
}

function saleText(loyalty) {
  if (money(loyalty)) return "promo $" + String(loyalty)
  return "no sale"
}

function searchExit(resultGen, currentGen, again) {
  if (resultGen === currentGen) return "apply"
  if (again) return "retry"
  return "drop"
}

function priced(name, price, loyalty) {
  var label = String(name || "")
  if (label.length < 1 || label.length > 120) return false
  if (/[<>&|]/.test(label)) return false
  if (!money(price)) return false
  var sale = String(loyalty || "")
  if (sale === "") return true
  return money(sale)
}

function hitReady(hit) {
  if (!hit) return false
  if (!/^[0-9]{12,14}$/.test(String(hit.id || ""))) return false
  return priced(hit.name, hit.price, hit.loyalty)
}

function storeTerm(text) {
  var term = String(text || "").replace(/[<>&]/g, "").trim()
  if (term.length < 3) return ""
  if (!/^[A-Za-z0-9][A-Za-z0-9 .'%\/-]{0,79}$/.test(term)) return ""
  return term
}

function storeZip(text) {
  var zip = String(text || "").trim()
  if (!/^[0-9]{5}$/.test(zip)) return ""
  return zip
}
