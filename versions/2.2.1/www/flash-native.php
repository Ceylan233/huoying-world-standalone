<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>火影世界 - 原生 Flash</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; border: 0; }
    html, body, iframe { width: 100%; height: 100%; overflow: hidden; background: #000; }
    iframe { display: block; }
  </style>
</head>
<body>
  <iframe id="native-page" title="火影世界原生 Flash"></iframe>
  <script>
    (function () {
      document.getElementById("native-page").src =
        "/flash-native.html" + window.location.search + window.location.hash;
    })();
  </script>
</body>
</html>
