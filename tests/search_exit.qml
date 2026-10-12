import QtQuick
import "../Model.js" as Model

QtObject {
  Component.onCompleted: {
    var rows = [
      [Model.searchExit(3, 3, false), "apply", "same"],
      [Model.searchExit(3, 3, true), "apply", "same-again"],
      [Model.searchExit(2, 3, true), "retry", "inflight"],
      [Model.searchExit(2, 3, false), "drop", "stale"],
      [Model.saleText("3.99"), "promo $3.99", "promo"],
      [Model.saleText(""), "no sale", "missing"],
      [Model.saleText("0.00"), "no sale", "zero"],
      [Model.saleText("nope"), "no sale", "bad"]
    ]
    for (var i = 0; i < rows.length; i++) {
      if (rows[i][0] !== rows[i][1]) {
        Qt.exit(10 + i)
        return
      }
    }
    Qt.exit(0)
  }
}
