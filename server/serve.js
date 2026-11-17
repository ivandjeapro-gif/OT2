const http = require("http");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const mime = {
  ".html": "text/html",
  ".css": "text/css",
  ".js": "text/javascript",
  ".mjs": "text/javascript",
  ".json": "application/json",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".geojson": "application/json",
  ".csv": "text/csv",
  ".mbtiles": "application/octet-stream"
};

const server = http.createServer((req, res) => {
  let p = decodeURIComponent(req.url.split("?")[0]);
  if (p === "/") p = "/index.html";

  const f = path.resolve(path.join(ROOT, p));

  // empeche le chemin hors du ROOT (directory traversal)
  if (f !== ROOT && !f.startsWith(ROOT + path.sep)) {
    res.writeHead(403);
    res.end("403");
    return;
  }

  fs.readFile(f, (err, data) => {
    if (err) {
      res.writeHead(404, { "Content-Type": "text/plain" });
      res.end("404");
      return;
    }
    const type = mime[path.extname(f).toLowerCase()] || "application/octet-stream";
    res.writeHead(200, {
      "Content-Type": type,
      // sans ce header, Chrome bloque les scripts locaux via ORB
      // et le HTML est rendu mais rien ne s'execute
      "Cross-Origin-Resource-Policy": "same-origin",
      "Cache-Control": "no-cache",
      // desactive le chunked encoding : certaines versions de Chrome
      // considerent alors la ressource comme "no-cors" opaque
      "Content-Length": String(data.length),
      "Connection": "close"
    });
    res.end(data);
    res.socket?.end();
  });
});

server.listen(8777, "127.0.0.1", () => console.log("UP"));
