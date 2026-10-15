import QtQuick
import QtQuick.Controls
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "jordan.grocy"
  ipcTarget: "jordan.grocy"

  readonly property string python: "/usr/bin/python3"
  readonly property string helper: Qt.resolvedUrl("bin/grocy.py").toString().replace(/^file:\/\//, "")
  readonly property color fg: bar ? bar.barForeground : Color.foreground
  readonly property color dim: Qt.darker(fg, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property var procEnv: ({
    "PATH": "/usr/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONNOUSERSITE": "1",
    "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt"
  })

  property var rows: []
  property var orderRows: []
  property string pendingProductId: ""
  property string pendingVerb: ""
  property bool orderRetried: false
  property string listName: "Next week's order"
  property string statusText: ""
  property bool statusBad: false
  property bool loadedOnce: false
  property bool busy: false
  property bool stockAgain: false
  property bool searchAgain: false
  property int selected: 0
  property int stockGen: 0
  property int actionGen: 0
  property int searchGen: 0
  property int storeSelected: 0
  property bool storeLoaded: false
  property string stockBuf: ""
  property string stockErr: ""
  property string actionBuf: ""
  property string actionErr: ""
  property string searchBuf: ""
  property string searchErr: ""
  property string pendingName: ""
  property string homeName: ""
  property string homeLine: ""
  property bool changingStore: false
  property bool krogerOn: true
  property int storeTotal: 0
  property int storeNext: 0
  property string storeTermDone: ""
  property int placeGen: 0
  property string placeBuf: ""
  property string placeErr: ""

  function clean(text) {
    return String(text || "").replace(/[<>&]/g, "").slice(0, 180)
  }

  function showStatus(text, bad) {
    root.statusText = root.clean(text)
    root.statusBad = bad === true
  }

  function rememberList(doc) {
    if (doc && typeof doc.list === "string" && /^Order [0-9]{4}-W[0-9]{2}$/.test(doc.list))
      root.listName = doc.list
  }

  function takeDoc(buf) {
    var text = String(buf || "").trim()
    if (!text || text.length > 300000) return null
    try {
      var doc = JSON.parse(text)
    } catch (e) {
      return null
    }
    if (!doc || typeof doc !== "object" || Array.isArray(doc)) return null
    return doc
  }

  function applyFilter() {
    var matched = Model.filterStock(root.rows, searchField.text)
    filtered.clear()
    for (var i = 0; i < matched.length; i++) filtered.append(matched[i])
    root.selected = 0
    if (filtered.count > 0) stockList.positionViewAtIndex(0, ListView.Beginning)
  }

  function install(items) {
    if (!Array.isArray(items) || items.length > 400) {
      root.showStatus("Stock response was not a list.", true)
      return
    }
    var cleanRows = []
    var seen = ({})
    for (var i = 0; i < items.length; i++) {
      var row = items[i]
      var id = row ? String(row.id) : ""
      if (!row || typeof row.name !== "string" || row.name.length < 1 || row.name.length > 180 || !/^[1-9][0-9]{0,9}$/.test(id) || seen[id]) {
        root.showStatus("Stock response had an unexpected row.", true)
        return
      }
      seen[id] = true
      cleanRows.push({ productId: id, name: row.name, amount: root.clean(row.amount).slice(0, 16) })
    }
    root.rows = cleanRows
    root.loadedOnce = true
    root.applyFilter()
    return true
  }

  function installOrder(items) {
    if (!Array.isArray(items) || items.length > 400) {
      root.showStatus("Order response was not a list.", true)
      return false
    }
    var cleanRows = []
    var seen = ({})
    for (var i = 0; i < items.length; i++) {
      var row = items[i]
      var id = row ? String(row.id) : ""
      if (/^s[1-9][0-9]{0,9}$/.test(id)) {
        var qty = typeof row.amount === "string" ? row.amount : "1"
        if (!row || typeof row.name !== "string" || !Model.priced(row.name, row.price, row.loyalty) || seen[id]) {
          root.showStatus("Order response had an unexpected row.", true)
          return false
        }
        seen[id] = true
        cleanRows.push({ productId: "", lineId: id, name: row.name, amount: root.clean(qty).slice(0, 16), price: row.price, loyalty: row.loyalty || "", source: "store" })
        continue
      }
      if (!row || typeof row.name !== "string" || row.name.length < 1 || row.name.length > 180 || !/^[1-9][0-9]{0,9}$/.test(id) || seen[id]) {
        root.showStatus("Order response had an unexpected row.", true)
        return false
      }
      seen[id] = true
      cleanRows.push({ productId: id, lineId: "", name: row.name, amount: root.clean(row.amount).slice(0, 16), price: "", loyalty: "", source: "stock" })
    }
    root.orderRows = cleanRows
    ordered.clear()
    for (var j = 0; j < cleanRows.length; j++) ordered.append(cleanRows[j])
    return true
  }

  function showOrder(rows) {
    root.orderRows = rows
    ordered.clear()
    for (var i = 0; i < rows.length; i++) ordered.append(rows[i])
  }

  function orderHas(items, productId) {
    var id = String(productId)
    for (var i = 0; i < items.length; i++) {
      if (items[i] && String(items[i].id) === id) return true
    }
    return false
  }

  function finishOrder(items) {
    if (!Array.isArray(items) || items.length > 400) {
      root.showStatus("Order response was not a list.", true)
      return false
    }
    var waiting = root.pendingProductId !== ""
    var present = waiting && root.orderHas(items, root.pendingProductId)
    var settled = !waiting || (root.pendingVerb === "remove" ? !present : present)
    if (waiting && !settled && !root.orderRetried) {
      root.orderRetried = true
      orderRetry.restart()
      return true
    }
    root.pendingProductId = ""
    root.orderRetried = false
    return root.installOrder(items)
  }

  function bumpOrder(productId, name, adding) {
    var id = String(productId)
    var next = []
    var found = false
    for (var i = 0; i < root.orderRows.length; i++) {
      var row = root.orderRows[i]
      if (!row || row.productId !== id) {
        if (row) next.push(row)
        continue
      }
      found = true
      if (!adding) continue
      var amount = parseFloat(row.amount)
      if (!isFinite(amount)) amount = 0
      next.push({ productId: id, lineId: row.lineId || "", name: row.name, amount: String(amount + 1), price: row.price || "", loyalty: row.loyalty || "", source: row.source || "stock" })
    }
    if (adding && !found) next.push({ productId: id, lineId: "", name: root.clean(name) || "Item", amount: "1", price: "", loyalty: "", source: "stock" })
    next.sort(function (a, b) {
      var left = String(a.name).toLowerCase()
      var right = String(b.name).toLowerCase()
      if (left < right) return -1
      if (left > right) return 1
      return 0
    })
    root.showOrder(next)
  }

  function loadStock() {
    if (stockProc.running) {
      root.stockAgain = true
      root.stockGen += 1
      return
    }
    root.stockAgain = false
    root.stockGen += 1
    stockProc.gen = root.stockGen
    root.stockBuf = ""
    root.stockErr = ""
    stockProc.running = true
    stockWatch.restart()
  }

  function startAction(verb, productId, name, extra) {
    if (root.busy || actionProc.running) return
    var id = String(productId)
    var storeAdd = verb === "add-store"
    var storeLine = verb === "store-qty" || verb === "store-take"
    var okId = storeAdd ? /^[0-9]{12,14}$/.test(id) : /^[1-9][0-9]{0,9}$/.test(id)
    var qty = String(extra || "")
    if (verb === "store-qty" && !/^[1-9][0-9]?$/.test(qty)) okId = false
    if (!okId) {
      root.showStatus(storeLine ? "That order row has no line id." : (verb === "remove" ? "That order row has no product id." : (storeAdd ? "That store row has no product id." : "That stock row has no product id.")), true)
      return
    }
    root.pendingName = root.clean(name)
    root.actionGen += 1
    actionProc.gen = root.actionGen
    actionProc.verb = verb
    actionProc.actionId = id
    actionProc.extra = verb === "store-qty" ? qty : ""
    root.actionBuf = ""
    root.actionErr = ""
    root.busy = true
    var label = root.pendingName
    if (verb === "remove" || verb === "store-take") root.showStatus("Taking " + label + " off…", false)
    else if (verb === "store-qty") root.showStatus("Setting " + label + " to " + qty + "…", false)
    else root.showStatus("Adding " + label + "…", false)
    actionProc.running = true
    actionWatch.restart()
  }

  function addHit(productId, name, price, loyalty) {
    if (!Model.hitReady({ id: productId, name: name, price: price, loyalty: loyalty })) {
      root.showStatus("That item needs a name and a price.", true)
      return
    }
    root.startAction("add-store", productId, name, "")
  }

  function storeLineNumber(lineId) {
    var id = String(lineId || "")
    if (!/^s[1-9][0-9]{0,9}$/.test(id)) return ""
    return id.slice(1)
  }

  function setStoreQty(lineId, amount, name, delta) {
    if (!/^[1-9][0-9]?$/.test(String(amount || ""))) return
    var next = parseInt(amount, 10) + delta
    if (next < 1 || next > 99) return
    var id = root.storeLineNumber(lineId)
    if (!id) return
    root.startAction("store-qty", id, name, String(next))
  }

  function takeOffStore(lineId, name) {
    var id = root.storeLineNumber(lineId)
    if (!id) {
      root.showStatus("That order row has no line id.", true)
      return
    }
    root.startAction("store-take", id, name, "")
  }

  function installHits(hits, append) {
    if (!Array.isArray(hits) || hits.length > 24) {
      root.showStatus("Store search was not a list.", true)
      return false
    }
    var adding = append === true
    var existing = Object.create(null)
    if (adding) {
      for (var e = 0; e < storeHits.count; e++) {
        var kept = storeHits.get(e)
        if (kept && kept.productId) existing[kept.productId] = true
      }
    }
    var clean = []
    var seen = Object.create(null)
    var room = 72 - (adding ? storeHits.count : 0)
    for (var i = 0; i < hits.length; i++) {
      var hit = hits[i]
      if (!hit || typeof hit.name !== "string" || hit.name.length > 120) {
        root.showStatus("Store search had an unexpected row.", true)
        return false
      }
      var id = String(hit.id || "")
      if (id && seen[id]) {
        root.showStatus("Store search had an unexpected row.", true)
        return false
      }
      if (id) seen[id] = true
      if (id && existing[id]) continue
      if (clean.length >= room) break
      var price = typeof hit.price === "string" ? hit.price : ""
      var loyalty = typeof hit.loyalty === "string" ? hit.loyalty : ""
      var ready = Model.hitReady({ id: id, name: hit.name, price: price, loyalty: loyalty })
      clean.push({
        productId: ready ? id : "",
        name: hit.name,
        price: Model.money(price) ? price : "",
        loyalty: Model.money(loyalty) ? loyalty : "",
        complete: ready ? "yes" : "no"
      })
    }
    if (!adding) {
      storeHits.clear()
      root.storeSelected = 0
    }
    for (var j = 0; j < clean.length; j++) storeHits.append(clean[j])
    root.storeLoaded = true
    return true
  }

  function runSearch(start) {
    var startAt = 0
    if (typeof start === "number" && isFinite(start) && start > 0 && start <= 240 && start === Math.floor(start))
      startAt = start
    var typedRaw = String(storeField.text || "").replace(/[<>&]/g, "").trim()
    if (startAt === 0 && typedRaw.length > 0 && typedRaw.length < 3) {
      root.showStatus("Enter at least 3 characters.", true)
      return
    }
    var typed = Model.storeTerm(storeField.text)
    if (startAt > 0 && typed !== root.storeTermDone) startAt = 0
    var term = startAt > 0 ? root.storeTermDone : typed
    if (!term) {
      root.showStatus("Enter a store item to search for.", true)
      return
    }
    if (searchProc.running) return
    root.searchAgain = false
    root.searchGen += 1
    searchProc.gen = root.searchGen
    searchProc.term = term
    searchProc.startAt = startAt
    searchProc.startText = String(startAt)
    root.searchBuf = ""
    root.searchErr = ""
    root.showStatus(startAt > 0 ? "Loading more…" : "Searching this store…", false)
    searchProc.running = true
    searchWatch.restart()
  }

  function placeName(value) {
    if (typeof value !== "string" || value.length < 1 || value.length > 80) return ""
    return root.clean(value)
  }

  function placeLine(line, city) {
    var street = typeof line === "string" && line.length <= 80 ? root.clean(line) : ""
    var town = typeof city === "string" && city.length <= 40 ? root.clean(city) : ""
    if (street && town) return street + ", " + town
    return street || town
  }

  function startPlace(verb, arg) {
    if (placeProc.running) return false
    root.placeGen += 1
    placeProc.gen = root.placeGen
    placeProc.verb = verb
    placeProc.arg = arg || ""
    root.placeBuf = ""
    root.placeErr = ""
    placeProc.running = true
    placeWatch.restart()
    return true
  }

  function loadHome() {
    root.startPlace("home-store", "")
  }

  function findStores() {
    var zip = Model.storeZip(zipField.text)
    if (!zip) {
      root.showStatus("Enter a 5-digit ZIP.", true)
      return
    }
    if (!root.startPlace("stores", zip)) return
    root.showStatus("Looking up stores…", false)
  }

  function installStores(stores) {
    if (!Array.isArray(stores) || stores.length > 8) {
      root.showStatus("Store list was not a list.", true)
      return false
    }
    var clean = []
    var seen = Object.create(null)
    for (var i = 0; i < stores.length; i++) {
      var row = stores[i]
      var id = row ? String(row.id || "") : ""
      var name = root.placeName(row && row.name)
      if (!/^[0-9]{8}$/.test(id) || !name || seen[id]) {
        root.showStatus("Store list had an unexpected row.", true)
        return false
      }
      seen[id] = true
      clean.push({
        storeId: id,
        name: name,
        line: typeof row.line === "string" ? root.placeLine(row.line, "") : "",
        city: typeof row.city === "string" && row.city.length <= 40 ? root.clean(row.city) : ""
      })
    }
    nearbyStores.clear()
    for (var j = 0; j < clean.length; j++) nearbyStores.append(clean[j])
    if (nearbyStores.count === 0) root.showStatus("No stores near that ZIP.", false)
    else root.statusText = ""
    return true
  }

  function useStore(storeId) {
    var id = String(storeId || "")
    if (!/^[0-9]{8}$/.test(id)) return
    if (!root.startPlace("use-store", id)) return
    root.showStatus("Saving this store…", false)
  }

  function forgetStorePrices() {
    root.searchAgain = searchProc.running
    root.searchGen += 1
    if (searchProc.running) searchProc.signal(15)
    storeHits.clear()
    nearbyStores.clear()
    root.storeLoaded = false
    root.storeTotal = 0
    root.storeNext = 0
    root.storeTermDone = ""
    root.storeSelected = 0
    root.changingStore = false
  }

  function choose(productId, name) {
    root.startAction("add", productId, name)
  }

  function takeOff(productId, name) {
    root.startAction("remove", productId, name)
  }

  function chooseSelected() {
    if (filtered.count < 1) {
      root.showStatus(root.rows.length === 0 ? "Nothing in stock." : "No matches.", true)
      return
    }
    var row = filtered.get(root.selected)
    if (!row) return
    root.choose(row.productId, row.name)
  }

  onOpenedChanged: {
    if (opened) {
      root.statusText = ""
      root.statusBad = false
      root.loadStock()
      root.loadHome()
    }
  }

  Component.onDestruction: {
    if (stockProc.running) stockProc.signal(15)
    if (actionProc.running) actionProc.signal(15)
    if (searchProc.running) searchProc.signal(15)
    if (placeProc.running) placeProc.signal(15)
  }

  ListModel { id: filtered }
  ListModel { id: ordered }
  ListModel { id: storeHits }
  ListModel { id: nearbyStores }

  Process {
    id: stockProc
    property int gen: 0
    command: [root.python, "-I", "-S", root.helper, "stock"]
    workingDirectory: "/"
    clearEnvironment: true
    environment: root.procEnv
    stdout: SplitParser {
      splitMarker: ""
      onRead: function (chunk) {
        root.stockBuf += chunk
        if (root.stockBuf.length > 300000) {
          root.stockBuf = ""
          root.stockGen += 1
          root.stockAgain = false
          root.showStatus("Stock response was too large.", true)
          stockProc.signal(15)
          stockKill.restart()
        }
      }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function (chunk) {
        if (root.stockErr.length < 4000) root.stockErr += chunk
      }
    }
    onExited: function (code, status) {
      stockWatch.stop()
      if (stockProc.gen !== root.stockGen) {
        if (root.stockAgain) root.loadStock()
        return
      }
      var doc = root.takeDoc(root.stockBuf)
      root.rememberList(doc)
      root.loadedOnce = true
      if (doc && doc.ok === true) {
        if (!root.install(doc.items)) return
        if (root.finishOrder(doc.order) && root.pendingProductId === "") root.statusText = ""
        return
      }
      if (doc && typeof doc.error === "string" && doc.error.length > 0) {
        root.showStatus(doc.error, true)
        return
      }
      root.showStatus("The order could not be loaded.", true)
    }
  }

  Process {
    id: actionProc
    property int gen: 0
    property string verb: "add"
    property string actionId: ""
    property string extra: ""
    command: actionProc.extra === ""
      ? [root.python, "-I", "-S", root.helper, actionProc.verb, actionProc.actionId]
      : [root.python, "-I", "-S", root.helper, actionProc.verb, actionProc.actionId, actionProc.extra]
    workingDirectory: "/"
    clearEnvironment: true
    environment: root.procEnv
    stdout: SplitParser {
      splitMarker: ""
      onRead: function (chunk) {
        root.actionBuf += chunk
        if (root.actionBuf.length > 300000) {
          root.actionBuf = ""
          root.actionGen += 1
          root.busy = false
          root.showStatus("The order response was too large.", true)
          actionProc.signal(15)
          actionKill.restart()
        }
      }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function (chunk) {
        if (root.actionErr.length < 4000) root.actionErr += chunk
      }
    }
    onExited: function (code, status) {
      actionWatch.stop()
      if (actionProc.gen !== root.actionGen) return
      root.busy = false
      var doc = root.takeDoc(root.actionBuf)
      root.rememberList(doc)
      if (doc && doc.ok === true) {
        var list = typeof doc.list === "string" ? doc.list : root.listName
        var name = root.pendingName || "item"
        if (actionProc.verb === "add-store" || actionProc.verb === "store-qty" || actionProc.verb === "store-take") {
          root.pendingProductId = ""
          root.pendingVerb = ""
          if (actionProc.verb === "store-take") root.showStatus("Took " + name + " off " + list + ".", false)
          else if (actionProc.verb === "store-qty") root.showStatus("Set " + name + " to " + actionProc.extra + ".", false)
          else root.showStatus("Added " + name + " to " + list + ".", false)
          root.loadStock()
          return
        }
        root.pendingProductId = actionProc.actionId
        root.pendingVerb = actionProc.verb
        root.orderRetried = false
        root.bumpOrder(actionProc.actionId, name, actionProc.verb !== "remove")
        if (actionProc.verb === "remove")
          root.showStatus("Took " + name + " off " + list + ".", false)
        else
          root.showStatus("Added " + name + " to " + list + ".", false)
        root.loadStock()
        return
      }
      if (doc && typeof doc.error === "string" && doc.error.length > 0) {
        root.showStatus(doc.error, true)
        return
      }
      root.showStatus("The order could not be loaded.", true)
    }
  }

  Timer {
    id: stockWatch
    interval: 12000
    onTriggered: {
      if (!stockProc.running) return
      root.stockGen += 1
      root.stockAgain = false
      root.showStatus("The order did not respond.", true)
      stockProc.signal(15)
      stockKill.restart()
    }
  }
  Timer {
    id: stockKill
    interval: 2000
    onTriggered: if (stockProc.running) stockProc.signal(9)
  }
  Timer {
    id: actionWatch
    interval: 36000
    onTriggered: {
      if (!actionProc.running) return
      root.actionGen += 1
      root.busy = false
      root.showStatus("The order did not respond.", true)
      actionProc.signal(15)
      actionKill.restart()
    }
  }
  Timer {
    id: actionKill
    interval: 2000
    onTriggered: if (actionProc.running) actionProc.signal(9)
  }
  Timer {
    id: orderRetry
    interval: 500
    onTriggered: root.loadStock()
  }

  Process {
    id: searchProc
    property int gen: 0
    property string term: ""
    property int startAt: 0
    property string startText: "0"
    command: [root.python, "-I", "-S", root.helper, "search", searchProc.term, searchProc.startText]
    workingDirectory: "/"
    clearEnvironment: true
    environment: root.procEnv
    stdout: SplitParser {
      splitMarker: ""
      onRead: function (chunk) {
        root.searchBuf += chunk
        if (root.searchBuf.length > 64000) {
          root.searchBuf = ""
          root.searchGen += 1
          root.searchAgain = false
          root.showStatus("Store search was too large.", true)
          searchProc.signal(15)
          searchKill.restart()
        }
      }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function (chunk) {
        if (root.searchErr.length < 4000) root.searchErr += chunk
      }
    }
    onExited: function (code, status) {
      searchWatch.stop()
      var decision = Model.searchExit(searchProc.gen, root.searchGen, root.searchAgain)
      if (decision !== "apply") {
        root.searchAgain = false
        if (decision === "retry") root.runSearch(0)
        return
      }
      var doc = root.takeDoc(root.searchBuf)
      if (doc && doc.ok === true && Array.isArray(doc.hits)) {
        var pageCount = doc.hits.length
        if (!root.installHits(doc.hits, searchProc.startAt > 0)) return
        root.storeTermDone = searchProc.term
        root.storeNext = searchProc.startAt + pageCount
        var shown = storeHits.count
        var total = shown
        var reported = doc.total
        if (typeof reported === "number" && isFinite(reported) && reported === Math.floor(reported) && reported >= shown && reported <= 5000)
          total = reported
        root.storeTotal = total
        if (shown === 0) root.showStatus("Nothing at this store matched.", false)
        else root.statusText = ""
        return
      }
      if (doc && typeof doc.error === "string" && doc.error.length > 0) {
        root.showStatus(doc.error, true)
        return
      }
      root.showStatus("Kroger request failed.", true)
    }
  }

  Timer {
    id: searchWatch
    interval: 20000
    onTriggered: {
      if (!searchProc.running) return
      root.searchGen += 1
      root.searchAgain = false
      root.showStatus("Kroger did not respond.", true)
      searchProc.signal(15)
      searchKill.restart()
    }
  }
  Timer {
    id: searchKill
    interval: 2000
    onTriggered: if (searchProc.running) searchProc.signal(9)
  }

  Process {
    id: placeProc
    property int gen: 0
    property string verb: "home-store"
    property string arg: ""
    command: placeProc.arg === ""
      ? [root.python, "-I", "-S", root.helper, placeProc.verb]
      : [root.python, "-I", "-S", root.helper, placeProc.verb, placeProc.arg]
    workingDirectory: "/"
    clearEnvironment: true
    environment: root.procEnv
    stdout: SplitParser {
      splitMarker: ""
      onRead: function (chunk) {
        root.placeBuf += chunk
        if (root.placeBuf.length > 16000) {
          root.placeBuf = ""
          root.placeGen += 1
          root.showStatus("Store list was too large.", true)
          placeProc.signal(15)
          placeKill.restart()
        }
      }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function (chunk) {
        if (root.placeErr.length < 4000) root.placeErr += chunk
      }
    }
    onExited: function (code, status) {
      placeWatch.stop()
      if (placeProc.gen !== root.placeGen) return
      var doc = root.takeDoc(root.placeBuf)
      if (doc && doc.ok === true) {
        if (placeProc.verb === "stores") {
          root.installStores(doc.stores)
          return
        }
        var name = root.placeName(doc.name)
        if (!name) {
          root.showStatus("That store could not be loaded.", true)
          return
        }
        root.homeName = name
        root.homeLine = root.placeLine(doc.line, doc.city)
        if (placeProc.verb === "use-store") {
          root.forgetStorePrices()
          root.showStatus("Home store is " + name + ".", false)
        }
        return
      }
      if (doc && typeof doc.error === "string" && doc.error.length > 0) {
        if (doc.error === "Kroger is not configured") {
          root.krogerOn = false
          root.homeName = ""
          root.homeLine = ""
          return
        }
        root.showStatus(doc.error, true)
        return
      }
      root.showStatus("Kroger request failed.", true)
    }
  }

  Timer {
    id: placeWatch
    interval: 20000
    onTriggered: {
      if (!placeProc.running) return
      root.placeGen += 1
      root.showStatus("Kroger did not respond.", true)
      placeProc.signal(15)
      placeKill.restart()
    }
  }
  Timer {
    id: placeKill
    interval: 2000
    onTriggered: if (placeProc.running) placeProc.signal(9)
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    foreground: fg
    tooltipText: "Food Appears"
    onPressed: function () { root.toggle() }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: storeField
    contentWidth: panel.fittedContentWidth(Style.space(460))
    contentHeight: panel.fittedContentHeight(body.implicitHeight, Style.space(720))

    Column {
      id: body
      width: parent.width
      spacing: Style.space(8)

      Text {
        text: "Food Appears"
        textFormat: Text.PlainText
        color: fg
        font.family: root.fontFamily
        font.pixelSize: Style.font.title
      }

      Row {
        width: parent.width
        spacing: Style.space(8)
        visible: root.krogerOn

        Text {
          width: parent.width - homeToggle.width - Style.space(8)
          text: root.homeName !== "" ? root.homeName : "Loading store…"
          textFormat: Text.PlainText
          color: fg
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          id: homeToggle
          text: root.changingStore ? "Close" : "Change"
          textFormat: Text.PlainText
          color: Color.accent
          font.family: root.fontFamily
          font.pixelSize: Style.font.body

          MouseArea {
            anchors.fill: parent
            onClicked: root.changingStore = !root.changingStore
          }
        }
      }

      Text {
        width: parent.width
        visible: root.krogerOn && root.homeLine !== ""
        text: root.homeLine
        textFormat: Text.PlainText
        color: dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        elide: Text.ElideRight
      }

      Row {
        width: parent.width
        spacing: Style.space(8)
        visible: root.krogerOn && root.changingStore

        TextField {
          id: zipField
          width: parent.width - zipSearch.width - Style.space(8)
          placeholderText: "ZIP"
          maximumLength: 5
          font.family: root.fontFamily
          Keys.onPressed: function (event) {
            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
              root.findStores()
              event.accepted = true
            }
          }
        }

        Text {
          id: zipSearch
          height: zipField.height
          text: "Stores"
          textFormat: Text.PlainText
          color: Color.accent
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          verticalAlignment: Text.AlignVCenter

          MouseArea {
            anchors.fill: parent
            enabled: !placeProc.running
            onClicked: root.findStores()
          }
        }
      }

      ListView {
        id: nearbyList
        width: parent.width
        visible: root.krogerOn && root.changingStore && nearbyStores.count > 0
        height: visible ? Math.min(Style.space(136), nearbyStores.count * Style.space(34)) : 0
        clip: true
        model: nearbyStores
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        delegate: Rectangle {
          required property int index
          required property string storeId
          required property string name
          required property string line
          required property string city
          readonly property bool canUse: /^[0-9]{8}$/.test(storeId)
          width: nearbyList.width
          height: Style.space(34)
          color: "transparent"

          Text {
            width: parent.width - useStoreLabel.implicitWidth - Style.space(12)
            height: parent.height
            anchors.left: parent.left
            anchors.leftMargin: Style.space(6)
            text: name
            textFormat: Text.PlainText
            color: fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            elide: Text.ElideRight
            verticalAlignment: Text.AlignVCenter
          }

          Text {
            id: useStoreLabel
            anchors.right: parent.right
            anchors.rightMargin: Style.space(6)
            anchors.verticalCenter: parent.verticalCenter
            text: canUse ? "Use" : ""
            textFormat: Text.PlainText
            color: canUse ? Color.accent : dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body

            MouseArea {
              anchors.fill: parent
              enabled: canUse && !placeProc.running
              onClicked: root.useStore(storeId)
            }
          }
        }
      }

      Row {
        width: parent.width
        spacing: Style.space(8)
        visible: root.krogerOn

        TextField {
          id: storeField
          width: parent.width - storeSearch.width - Style.space(8)
          placeholderText: "Search this store"
          maximumLength: 80
          font.family: root.fontFamily
          Keys.onPressed: function (event) {
            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
              root.runSearch()
              event.accepted = true
            }
          }
        }

        Text {
          id: storeSearch
          height: storeField.height
          text: "Search"
          textFormat: Text.PlainText
          color: Color.accent
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          verticalAlignment: Text.AlignVCenter

          MouseArea {
            anchors.fill: parent
            enabled: !searchProc.running
            onClicked: root.runSearch()
          }
        }
      }

      Text {
        width: parent.width
        visible: root.krogerOn && !root.storeLoaded && storeHits.count === 0 && !searchProc.running
        text: "Search to see prices at this store."
        textFormat: Text.PlainText
        color: dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }

      ListView {
        id: storeList
        width: parent.width
        height: storeHits.count > 0 ? Math.min(Style.space(238), storeHits.count * Style.space(34)) : 0
        visible: root.krogerOn && storeHits.count > 0
        clip: true
        model: storeHits
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        delegate: Rectangle {
          required property int index
          required property string productId
          required property string name
          required property string price
          required property string loyalty
          required property string complete
          readonly property bool canAdd: complete === "yes" && Model.hitReady({ id: productId, name: name, price: price, loyalty: loyalty })
          width: storeList.width
          height: Style.space(34)
          color: index === root.storeSelected ? Qt.rgba(fg.r, fg.g, fg.b, 0.16) : "transparent"

          Text {
            id: hitName
            width: parent.width - hitSide.implicitWidth - Style.space(12)
            height: parent.height
            anchors.left: parent.left
            anchors.leftMargin: Style.space(6)
            text: name
            textFormat: Text.PlainText
            color: fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            elide: Text.ElideRight
            verticalAlignment: Text.AlignVCenter
          }

          Row {
            id: hitSide
            anchors.right: parent.right
            anchors.rightMargin: Style.space(6)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(8)

            Text {
              text: price !== "" ? ("$" + price) : "no price"
              textFormat: Text.PlainText
              color: dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }

            Text {
              text: Model.saleText(loyalty)
              textFormat: Text.PlainText
              color: dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }

            Text {
              text: "Add"
              textFormat: Text.PlainText
              color: canAdd ? Color.accent : dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body

              MouseArea {
                anchors.fill: parent
                enabled: canAdd && !root.busy
                onClicked: root.addHit(productId, name, price, loyalty)
              }
            }
          }
        }
      }

      Row {
        width: parent.width
        spacing: Style.space(8)
        visible: root.krogerOn && storeHits.count > 0

        Text {
          text: root.storeTotal > storeHits.count ? (storeHits.count + " of " + root.storeTotal) : (storeHits.count + " at this store")
          textFormat: Text.PlainText
          color: dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        Text {
          visible: storeHits.count < 72 && root.storeNext < root.storeTotal && storeHits.count < root.storeTotal
          text: searchProc.running ? "Loading…" : "More"
          textFormat: Text.PlainText
          color: Color.accent
          font.family: root.fontFamily
          font.pixelSize: Style.font.body

          MouseArea {
            anchors.fill: parent
            enabled: !searchProc.running && storeHits.count < 72 && root.storeNext < root.storeTotal
            onClicked: root.runSearch(root.storeNext)
          }
        }
      }

      Text {
        width: parent.width
        visible: root.krogerOn && root.storeLoaded && storeHits.count === 0
        text: "Nothing at this store matched."
        textFormat: Text.PlainText
        color: dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }

      Text {
        width: parent.width
        visible: root.statusText !== ""
        text: root.statusText
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        color: root.statusBad ? fg : Color.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }

      Rectangle {
        id: orderBox
        width: parent.width
        radius: Style.cornerRadius
        color: Qt.rgba(fg.r, fg.g, fg.b, 0.08)
        implicitHeight: orderPad.implicitHeight + Style.space(16)
        height: implicitHeight

        Column {
          id: orderPad
          x: Style.space(8)
          y: Style.space(8)
          width: parent.width - Style.space(16)
          spacing: Style.space(6)

          Row {
            width: parent.width

            Text {
              width: parent.width - orderCount.width
              height: orderCount.height
              text: root.listName
              textFormat: Text.PlainText
              color: fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              font.bold: true
              verticalAlignment: Text.AlignVCenter
            }

            Text {
              id: orderCount
              text: ordered.count > 0 ? String(ordered.count) : ""
              textFormat: Text.PlainText
              color: dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              verticalAlignment: Text.AlignVCenter
            }
          }

          Text {
            width: parent.width
            visible: ordered.count === 0
            text: root.loadedOnce ? "Nothing on this order yet." : "Loading this order…"
            textFormat: Text.PlainText
            color: dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.WordWrap
          }

          Flickable {
            id: orderFlick
            width: parent.width
            visible: ordered.count > 0
            height: Math.min(orderCol.implicitHeight, Style.space(140))
            contentWidth: width
            contentHeight: orderCol.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

            Column {
              id: orderCol
              width: orderFlick.width
              spacing: Style.space(4)

              Repeater {
                model: ordered

                delegate: Item {
                  required property string productId
                  required property string lineId
                  required property string name
                  required property string amount
                  required property string price
                  required property string loyalty
                  required property string source
                  readonly property bool canDown: /^[1-9][0-9]?$/.test(amount) && amount !== "1"
                  readonly property bool canUp: /^[1-9][0-9]?$/.test(amount) && amount !== "99"
                  width: orderCol.width
                  height: Math.max(nameText.implicitHeight, Style.space(34))

                  Text {
                    id: nameText
                    width: parent.width - sideRow.implicitWidth - Style.space(8)
                    text: name
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                    color: fg
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                  }

                  Row {
                    id: sideRow
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Style.space(8)

                    Text {
                      visible: source !== "store"
                      text: amount
                      textFormat: Text.PlainText
                      color: dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                    }

                    Text {
                      visible: source === "store"
                      text: "-"
                      textFormat: Text.PlainText
                      color: canDown ? Color.accent : dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body

                      MouseArea {
                        anchors.fill: parent
                        enabled: !root.busy && canDown
                        onClicked: root.setStoreQty(lineId, amount, name, -1)
                      }
                    }

                    Text {
                      visible: source === "store"
                      text: amount
                      textFormat: Text.PlainText
                      color: fg
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                    }

                    Text {
                      visible: source === "store"
                      text: "+"
                      textFormat: Text.PlainText
                      color: canUp ? Color.accent : dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body

                      MouseArea {
                        anchors.fill: parent
                        enabled: !root.busy && canUp
                        onClicked: root.setStoreQty(lineId, amount, name, 1)
                      }
                    }

                    Text {
                      visible: source === "store"
                      text: "$" + price
                      textFormat: Text.PlainText
                      color: dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                    }

                    Text {
                      visible: source === "store"
                      text: Model.saleText(loyalty)
                      textFormat: Text.PlainText
                      color: dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                    }

                    Text {
                      text: "Take off"
                      textFormat: Text.PlainText
                      color: Color.accent
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body

                      MouseArea {
                        anchors.fill: parent
                        enabled: !root.busy
                        onClicked: source === "store" ? root.takeOffStore(lineId, name) : root.takeOff(productId, name)
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }

      Text {
        width: parent.width
        text: "In stock"
        textFormat: Text.PlainText
        color: dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }

      TextField {
        id: searchField
        width: parent.width
        placeholderText: "Search stock"
        maximumLength: 80
        font.family: root.fontFamily
        onTextChanged: root.applyFilter()
        Keys.onPressed: function (event) {
          if (event.key === Qt.Key_Down) {
            root.selected = Math.min(filtered.count - 1, root.selected + 1)
            if (filtered.count > 0) stockList.positionViewAtIndex(root.selected, ListView.Contain)
            event.accepted = true
          } else if (event.key === Qt.Key_Up) {
            root.selected = Math.max(0, root.selected - 1)
            if (filtered.count > 0) stockList.positionViewAtIndex(root.selected, ListView.Contain)
            event.accepted = true
          } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            root.chooseSelected()
            event.accepted = true
          }
        }
      }

      ListView {
        id: stockList
        width: parent.width
        height: Style.space(160)
        clip: true
        model: filtered
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        delegate: Rectangle {
          required property int index
          required property string productId
          required property string name
          required property string amount
          width: stockList.width
          height: Style.space(34)
          color: index === root.selected ? Qt.rgba(fg.r, fg.g, fg.b, 0.16) : "transparent"

          Row {
            anchors.fill: parent
            anchors.leftMargin: Style.space(6)
            anchors.rightMargin: Style.space(6)
            spacing: Style.space(8)

            Text {
              width: parent.width - amountLabel.width - Style.space(8)
              height: parent.height
              text: name
              textFormat: Text.PlainText
              color: fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              elide: Text.ElideRight
              verticalAlignment: Text.AlignVCenter
            }

            Text {
              id: amountLabel
              height: parent.height
              text: amount
              textFormat: Text.PlainText
              color: dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              verticalAlignment: Text.AlignVCenter
            }
          }

          MouseArea {
            anchors.fill: parent
            enabled: !root.busy
            onClicked: root.choose(productId, name)
          }
        }
      }

      Text {
        width: parent.width
        visible: filtered.count === 0
        text: root.loadedOnce ? (root.rows.length === 0 ? "Nothing in stock." : "No matches.") : "Loading stock…"
        textFormat: Text.PlainText
        color: dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }
    }
  }
}
