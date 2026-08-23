/**
 * HadeelBeauty — Google Sheets order webhook
 *
 * 1. Open your sheet "Order HadeelBeauty" (tab: Feuille 1)
 * 2. Row 1 headers must be:
 *    date | order id | wilaya | baladia | name | phone | product | sku |
 *    quantity | total price | delivery location
 * 3. Extensions → Apps Script → paste this file → Save
 * 4. Deploy → New deployment → Web app
 *    - Execute as: Me
 *    - Who has access: Anyone
 * 5. Copy the /exec URL into Easypanel:
 *    GOOGLE_SHEETS_WEBHOOK_URL=https://script.google.com/macros/s/....../exec
 */

const SHEET_NAME = "Feuille 1";

const HEADERS = [
  "date",
  "order id",
  "wilaya",
  "baladia",
  "name",
  "phone",
  "product",
  "sku",
  "quantity",
  "total price",
  "delivery location",
];

function valueForHeader_(header, body) {
  switch (header) {
    case "date":
      return pick_(body, ["date"]);
    case "order id":
      return pick_(body, ["order id", "orderId", "order_id"]);
    case "wilaya":
      return pick_(body, ["wilaya"]);
    case "baladia":
      return pick_(body, ["baladia", "city"]);
    case "name":
      return pick_(body, ["name"]);
    case "phone":
      return pick_(body, ["phone"]);
    case "product":
      return pick_(body, ["product"]);
    case "sku":
      return pick_(body, ["sku"]);
    case "quantity":
      return pick_(body, ["quantity"]);
    case "total price":
      return pick_(body, ["total price", "totalPrice", "total_price"]);
    case "delivery location":
      return pick_(body, ["delivery location", "deliveryLocation", "address"]);
    default:
      return "";
  }
}

function rowFromPayload_(body) {
  return HEADERS.map(function (header) {
    return valueForHeader_(header, body);
  });
}

function doPost(e) {
  try {
    const body = parseBody_(e);
    const sheet = getSheet_();
    if (!sheet) {
      return jsonResponse_({ ok: false, error: "Sheet not found: " + SHEET_NAME });
    }

    ensureHeaders_(sheet);
    const rowData = rowFromPayload_(body);
    sheet.appendRow(rowData);

    return jsonResponse_({ ok: true, action: "append", orderId: pick_(body, ["order id", "orderId", "order_id"]) });
  } catch (err) {
    return jsonResponse_({ ok: false, error: String(err) });
  }
}

function doGet() {
  return jsonResponse_({ ok: true, service: "hadeelbeauty-sheets-webhook", method: "POST orders here" });
}

function parseBody_(e) {
  if (!e || !e.postData || !e.postData.contents) {
    throw new Error("Missing POST body");
  }
  return JSON.parse(e.postData.contents);
}

function getSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  return ss.getSheetByName(SHEET_NAME) || ss.getSheets()[0] || null;
}

function ensureHeaders_(sheet) {
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADERS);
    return;
  }

  const width = Math.max(sheet.getLastColumn(), HEADERS.length);
  const existing = sheet.getRange(1, 1, 1, width).getValues()[0];
  if (!headersMatch_(existing)) {
    sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]);
  }
}

function headersMatch_(existing) {
  for (var i = 0; i < HEADERS.length; i++) {
    if (String(existing[i] || "").trim().toLowerCase() !== HEADERS[i]) {
      return false;
    }
  }
  return true;
}

function pick_(body, keys, fallback) {
  for (var i = 0; i < keys.length; i++) {
    var key = keys[i];
    if (Object.prototype.hasOwnProperty.call(body, key) && body[key] !== null && body[key] !== undefined) {
      return String(body[key]);
    }
  }
  return fallback !== undefined ? String(fallback) : "";
}

function jsonResponse_(payload) {
  return ContentService.createTextOutput(JSON.stringify(payload)).setMimeType(
    ContentService.MimeType.JSON
  );
}
