/* 테마: 첫 그림부터 맞춰 그린다(밝은 화면이 잠깐 비치지 않게). 고른 값 csl.theme — system·light·dark */
(function () { var p; try { p = JSON.parse(localStorage.getItem("csl.theme")); } catch (e) {}
  document.documentElement.dataset.theme = p === "dark" || (p !== "light" && matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light"; })();
