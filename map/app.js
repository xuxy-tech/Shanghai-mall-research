(() => {
  "use strict";

  const data = window.MALL_MAP_DATA;
  if (!data || !Array.isArray(data.malls)) return;

  const byId = (id) => document.getElementById(id);
  const malls = [...data.malls].sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
  const list = byId("mall-list");
  const search = byId("mall-search");
  const indexView = byId("index-view");
  const detailView = byId("detail-view");
  const markers = new Map();
  const mallByName = new Map(malls.map((mall) => [mall.name, mall]));
  const relatedByMall = new Map(malls.map((mall) => [mall.name, []]));
  for (const relation of data.relationships) {
    relatedByMall.get(relation.a).push({ ...relation, peer: relation.b });
    relatedByMall.get(relation.b).push({ ...relation, peer: relation.a });
  }
  let selected = null;
  let map = null;
  let overviewMap = null;
  let overviewViewport = null;
  let clusters = null;
  let current = malls;
  let relationMetric = "brandJaccard";
  let relationLines = null;
  let shownStores = 40;

  const minStores = Math.min(...malls.map((mall) => mall.storeCount));
  function markerDiameter(count) {
    return Math.round(10 * Math.sqrt(count / minStores));
  }

  byId("top-mall-count").textContent = data.mallCount.toLocaleString("zh-CN");
  byId("top-store-count").textContent = data.storeCount.toLocaleString("zh-CN");

  function percent(value) {
    return `${(value * 100).toFixed(1)}%`;
  }

  function shares(mall) {
    return [mall.diningShare, mall.retailShare, Math.max(0, 1 - mall.diningShare - mall.retailShare)];
  }

  function compositionColor(values) {
    const peak = Math.max(...values);
    const channels = values.map((share) => Math.round(220 * share / peak));
    return `rgb(${channels.join(",")})`;
  }

  function mallColor(mall) {
    return compositionColor(shares(mall));
  }

  function drawTernaryLegend() {
    const canvas = byId("ternary-canvas");
    const ctx = canvas.getContext("2d");
    const vertices = [[76, 7], [8, 108], [144, 108]];
    const [top, left, right] = vertices;
    const denominator = (left[1] - right[1]) * (top[0] - right[0]) + (right[0] - left[0]) * (top[1] - right[1]);
    const image = ctx.createImageData(canvas.width, canvas.height);
    for (let y = 0; y < canvas.height; y += 1) {
      for (let x = 0; x < canvas.width; x += 1) {
        const dining = ((left[1] - right[1]) * (x - right[0]) + (right[0] - left[0]) * (y - right[1])) / denominator;
        const retail = ((right[1] - top[1]) * (x - right[0]) + (top[0] - right[0]) * (y - right[1])) / denominator;
        const other = 1 - dining - retail;
        if (Math.min(dining, retail, other) < 0) continue;
        const color = [dining, retail, other];
        const peak = Math.max(...color);
        const index = (y * canvas.width + x) * 4;
        for (let channel = 0; channel < 3; channel += 1) image.data[index + channel] = Math.round(220 * color[channel] / peak);
        image.data[index + 3] = 255;
      }
    }
    ctx.putImageData(image, 0, 0);
    ctx.beginPath();
    ctx.moveTo(...top);
    ctx.lineTo(...left);
    ctx.lineTo(...right);
    ctx.closePath();
    ctx.strokeStyle = "#63736d";
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  drawTernaryLegend();
  if (window.matchMedia("(max-width: 800px)").matches) byId("ternary-canvas").closest("details").open = false;

  function renderList() {
    list.replaceChildren();
    byId("visible-count").textContent = String(current.length);
    byId("map-status").textContent = search.value.trim()
      ? `搜索结果 · ${current.length} 个点位`
      : `全部商场 · ${data.mallCount} 个点位`;

    if (!current.length) {
      const empty = document.createElement("p");
      empty.className = "empty-search";
      empty.textContent = "没有匹配的商场";
      list.append(empty);
      return;
    }

    for (const mall of current) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "mall-row";
      button.title = `查看 ${mall.name}`;
      button.style.setProperty("--mall-color", mallColor(mall));
      const label = document.createElement("span");
      const name = document.createElement("strong");
      name.textContent = mall.name;
      const swatch = document.createElement("i");
      swatch.className = "mall-swatch";
      const district = document.createElement("small");
      district.textContent = mall.district || "上海";
      const count = document.createElement("span");
      count.className = "store-number";
      count.textContent = mall.storeCount.toLocaleString("zh-CN");
      label.append(swatch, name, district);
      button.append(label, count);
      button.addEventListener("click", () => showMall(mall));
      list.append(button);
    }
  }

  function setTab(name) {
    for (const button of document.querySelectorAll(".detail-tab")) {
      const active = button.dataset.tab === name;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-current", active ? "page" : "false");
    }
    for (const panel of document.querySelectorAll(".tab-content")) panel.hidden = panel.id !== `tab-${name}`;
    if (selected) {
      updateMapMarkers();
      renderRelations();
      if (name === "relations") fitRelationBounds();
    }
    detailView.scrollTop = 0;
  }
  for (const button of document.querySelectorAll(".detail-tab")) {
    button.addEventListener("click", () => setTab(button.dataset.tab));
  }

  function renderStores() {
    if (!selected) return;
    const query = byId("store-search").value.trim().toLocaleLowerCase("zh-CN");
    const category = byId("store-category").value;
    const floor = byId("store-floor").value;
    const matches = selected.stores.filter((store) =>
      (!query || `${store.name} ${store.brand}`.toLocaleLowerCase("zh-CN").includes(query)) &&
      (!category || store.group === category) && (!floor || store.floor === floor)
    );
    byId("store-results").textContent = `${matches.length} / ${selected.storeCount} 家`;
    const container = byId("store-list");
    container.replaceChildren();
    for (const store of matches.slice(0, shownStores)) {
      const row = document.createElement("div");
      row.className = "store-row";
      const title = document.createElement("strong");
      title.textContent = store.name;
      const meta = document.createElement("small");
      meta.textContent = [store.category || store.group, store.floor, store.rating ? `${store.rating.toFixed(1)} 分` : ""].filter(Boolean).join(" · ");
      row.append(title, meta);
      container.append(row);
    }
    if (!matches.length) container.textContent = "没有匹配的门店";
    byId("store-more").hidden = matches.length <= shownStores;
  }

  function populateStoreControls(mall) {
    byId("store-search").value = "";
    shownStores = 40;
    for (const [id, values] of [
      ["store-category", mall.stores.map((store) => store.group).filter(Boolean)],
      ["store-floor", mall.stores.map((store) => store.floor).filter(Boolean)],
    ]) {
      const select = byId(id);
      select.replaceChildren(new Option(id === "store-floor" ? "全部楼层" : "全部业态", ""));
      for (const value of [...new Set(values)].sort((a, b) => a.localeCompare(b, "zh-CN", { numeric: true }))) {
        select.add(new Option(value, value));
      }
    }
    renderStores();
  }
  for (const id of ["store-search", "store-category", "store-floor"]) {
    byId(id).addEventListener(id === "store-search" ? "input" : "change", () => { shownStores = 40; renderStores(); });
  }
  byId("store-more").addEventListener("click", () => { shownStores += 40; renderStores(); });

  function topRelations(mall) {
    return [...relatedByMall.get(mall.name)]
      .filter((relation) => relation[relationMetric] > 0)
      .sort((a, b) => b[relationMetric] - a[relationMetric] || b.sharedCount - a.sharedCount)
      .slice(0, 3);
  }

  function updateMapMarkers() {
    if (!clusters) return;
    const visible = new Set(current.map((mall) => mall.name));
    if (selected) visible.add(selected.name);
    if (selected && byId("tab-relations").hidden === false) {
      for (const relation of topRelations(selected)) visible.add(relation.peer);
    }
    clusters.clearLayers();
    for (const name of visible) clusters.addLayer(markers.get(name));
    byId("map-status").textContent = selected && !byId("tab-relations").hidden
      ? `关联视图 · ${visible.size} 个点位`
      : search.value.trim() ? `搜索结果 · ${current.length} 个点位` : `全部商场 · ${data.mallCount} 个点位`;
  }

  function fitRelationBounds() {
    if (!selected || !map) return;
    const points = [selected, ...topRelations(selected).map((relation) => mallByName.get(relation.peer))];
    map.stop();
    map.fitBounds(L.latLngBounds(points.map((mall) => mall.coordinates)).pad(0.18), {
      padding: [50, 50], maxZoom: 12, animate: false,
    });
  }

  function renderRelations() {
    if (!selected) return;
    if (relationLines) relationLines.clearLayers();
    const container = byId("relation-list");
    container.replaceChildren();
    for (const relation of topRelations(selected)) {
      const peer = mallByName.get(relation.peer);
      if (relationLines && !byId("tab-relations").hidden) {
        L.polyline([selected.coordinates, peer.coordinates], {
          color: "#193f39", weight: 2, opacity: 0.75, dashArray: "5 5", interactive: false,
        }).addTo(relationLines);
      }
      const entry = document.createElement("div");
      entry.className = "relation-entry";
      const row = document.createElement("button");
      row.type = "button";
      row.className = "relation-row";
      const title = document.createElement("strong");
      title.textContent = peer.name;
      const score = document.createElement("span");
      score.textContent = relationMetric === "brandJaccard"
        ? `共享 ${relation.sharedCount} 个品牌 · Jaccard ${(relation.brandJaccard * 100).toFixed(1)}%`
        : `业态余弦 ${(relation.categoryCosine * 100).toFixed(1)}% · 共享 ${relation.sharedCount} 个品牌`;
      const evidence = document.createElement("small");
      evidence.textContent = relation.sharedBrands.length
        ? `共同品牌：${relation.sharedBrands.slice(0, 5).join("、")}${relation.sharedBrands.length > 5 ? "…" : ""}`
        : "当前样本没有共同品牌";
      row.append(title, score, evidence);
      row.addEventListener("click", () => showMall(peer));
      entry.append(row);
      if (relation.sharedBrands.length > 5) {
        const details = document.createElement("details");
        details.className = "shared-brands";
        const summary = document.createElement("summary");
        summary.textContent = `查看全部 ${relation.sharedBrands.length} 个共同品牌`;
        const names = document.createElement("p");
        names.textContent = relation.sharedBrands.join("、");
        details.append(summary, names);
        entry.append(details);
      }
      container.append(entry);
    }
  }
  for (const button of document.querySelectorAll(".relation-modes button")) {
    button.addEventListener("click", () => {
      relationMetric = button.dataset.metric;
      for (const mode of document.querySelectorAll(".relation-modes button")) mode.classList.toggle("is-active", mode === button);
      updateMapMarkers();
      renderRelations();
      if (!byId("tab-relations").hidden) fitRelationBounds();
    });
  }

  function populateDetail(mall) {
    byId("detail-name").textContent = mall.name;
    byId("detail-color").style.background = mallColor(mall);
    byId("detail-place").textContent = [mall.district, mall.businessArea, mall.address].filter(Boolean).join(" · ");
    byId("detail-stores").textContent = mall.storeCount.toLocaleString("zh-CN");
    byId("detail-brands").textContent = mall.brandCount.toLocaleString("zh-CN");
    byId("detail-breadth").textContent = mall.categoryBreadth.toLocaleString("zh-CN");

    const other = Math.max(0, 1 - mall.retailShare - mall.diningShare);
    const bar = byId("share-bar");
    bar.replaceChildren();
    bar.setAttribute("aria-label", `购物 ${percent(mall.retailShare)}，美食 ${percent(mall.diningShare)}，其他 ${percent(other)}`);
    for (const [kind, share] of [["retail", mall.retailShare], ["dining", mall.diningShare], ["other", other]]) {
      const segment = document.createElement("span");
      segment.className = kind;
      segment.style.width = `${share * 100}%`;
      bar.append(segment);
    }
    byId("detail-retail").textContent = percent(mall.retailShare);
    byId("detail-dining").textContent = percent(mall.diningShare);
    byId("detail-other").textContent = percent(other);

    const categoryList = byId("category-list");
    categoryList.replaceChildren();
    const comparisons = Object.entries(mall.categories)
      .filter(([label, count]) => label !== "其它" && count >= 3)
      .map(([label, count]) => ({
        label, count,
        share: count / mall.storeCount,
        difference: count / mall.storeCount - data.categoryTotals[label] / data.storeCount,
      }))
      .sort((a, b) => b.difference - a.difference)
      .slice(0, 5);
    for (const { label, count, share, difference } of comparisons) {
      const item = document.createElement("div");
      item.className = "category-item";
      const head = document.createElement("div");
      head.className = "category-item-head";
      const name = document.createElement("span");
      name.textContent = label;
      const value = document.createElement("strong");
      value.textContent = `${count} 家 · ${percent(share)}`;
      const comparison = document.createElement("small");
      comparison.className = "category-comparison";
      comparison.textContent = `高于全样本 ${(difference * 100).toFixed(1)} 个百分点`;
      const track = document.createElement("div");
      track.className = "category-track";
      const fill = document.createElement("span");
      fill.style.width = `${share * 100}%`;
      head.append(name, value);
      track.append(fill);
      item.append(head, track, comparison);
      categoryList.append(item);
    }
    populateStoreControls(mall);
  }

  function showMall(mall) {
    if (selected && markers.has(selected.name)) {
      markers.get(selected.name).getElement()?.classList.remove("is-selected");
    }
    selected = mall;
    populateDetail(mall);
    setTab("profile");
    indexView.hidden = true;
    detailView.hidden = false;
    detailView.scrollTop = 0;
    const marker = markers.get(mall.name);
    if (marker && map) {
      const focus = () => {
        if (selected !== mall) return;
        marker.getElement()?.classList.add("is-selected");
        if (!byId("tab-relations").hidden) {
          fitRelationBounds();
          return;
        }
        map.setView(marker.getLatLng(), Math.max(map.getZoom(), 12), { animate: false });
      };
      focus();
    }
  }

  function hideDetail() {
    if (selected && markers.has(selected.name)) {
      markers.get(selected.name).getElement()?.classList.remove("is-selected");
    }
    selected = null;
    if (relationLines) relationLines.clearLayers();
    updateMapMarkers();
    detailView.hidden = true;
    indexView.hidden = false;
    search.focus();
  }

  function updateFilter() {
    const query = search.value.trim().toLocaleLowerCase("zh-CN");
    current = malls.filter((mall) =>
      [mall.name, mall.district, mall.businessArea].some((part) => part.toLocaleLowerCase("zh-CN").includes(query))
    );
    renderList();
    updateMapMarkers();
    if (overviewMap) {
      overviewMap.eachLayer((layer) => {
        if (layer.options?.mallName) layer.setStyle({ opacity: current.some((mall) => mall.name === layer.options.mallName) ? 1 : 0.18, fillOpacity: current.some((mall) => mall.name === layer.options.mallName) ? 1 : 0.18 });
      });
    }
  }

  search.addEventListener("input", updateFilter);
  byId("back-button").addEventListener("click", hideDetail);

  if (typeof L === "undefined") {
    byId("map-error").hidden = false;
    renderList();
    return;
  }

  map = L.map("map", { zoomControl: false, scrollWheelZoom: true, preferCanvas: true, fadeAnimation: false });
  L.control.zoom({ position: "bottomright" }).addTo(map);
  const basemaps = [
    {
      url: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
      labels: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}",
      attribution: 'Tiles &copy; Esri, HERE, Garmin, OpenStreetMap contributors, GIS user community',
    },
    {
      url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
      attribution: 'Tiles &copy; Esri, HERE, Garmin, OpenStreetMap contributors, GIS user community',
    },
  ];
  let basemapIndex = 0;
  function loadBasemap() {
    const config = basemaps[basemapIndex];
    let failedTiles = 0;
    const labels = config.labels ? L.tileLayer(config.labels, { maxZoom: 18, pane: "tilePane" }) : null;
    const layer = L.tileLayer(config.url, {
      attribution: config.attribution,
      maxZoom: 18,
      subdomains: "abcd",
      detectRetina: false,
    });
    layer.on("tileerror", () => {
      failedTiles += 1;
      if (failedTiles !== 6) return;
      map.removeLayer(layer);
      if (labels) map.removeLayer(labels);
      if (basemapIndex < basemaps.length - 1) {
        basemapIndex += 1;
        loadBasemap();
      } else {
        byId("map-error").hidden = false;
      }
    });
    layer.addTo(map);
    if (labels) labels.addTo(map);
  }
  loadBasemap();

  const markerIcon = (mall) => {
    const diameter = markerDiameter(mall.storeCount);
    const hitDiameter = Math.max(30, diameter);
    return L.divIcon({
      className: "mall-marker",
      html: `<span style="width:${diameter}px;height:${diameter}px;background:${mallColor(mall)}"></span>`,
      iconSize: [hitDiameter, hitDiameter],
      iconAnchor: [hitDiameter / 2, hitDiameter / 2],
    });
  };
  clusters = typeof L.markerClusterGroup === "function"
    ? L.markerClusterGroup({
        showCoverageOnHover: false,
        spiderfyOnMaxZoom: true,
        disableClusteringAtZoom: 12,
        maxClusterRadius: 45,
        iconCreateFunction: (cluster) => {
          const size = cluster.getChildCount();
          const members = cluster.getAllChildMarkers().map((marker) => marker.mall);
          const mean = [0, 1, 2].map((channel) => members.reduce((sum, mall) => sum + shares(mall)[channel], 0) / size);
          return L.divIcon({
            className: `cluster-marker${size >= 10 ? " cluster-large" : ""}`,
            html: `<span style="background:${compositionColor(mean)}">${size}</span>`,
            iconSize: size >= 10 ? [45, 45] : [37, 37],
          });
        },
      })
    : L.layerGroup();

  for (const mall of malls) {
    const marker = L.marker(mall.coordinates, { icon: markerIcon(mall), title: mall.name, keyboard: true });
    marker.mall = mall;
    marker.bindTooltip(mall.name, { direction: "top", offset: [0, -9] });
    marker.on("click", () => showMall(mall));
    markers.set(mall.name, marker);
  }
  for (const marker of markers.values()) clusters.addLayer(marker);
  map.addLayer(clusters);
  relationLines = L.layerGroup().addTo(map);

  const allBounds = L.latLngBounds(malls.map((mall) => mall.coordinates));
  const sortedLatitudes = malls.map((mall) => mall.coordinates[0]).sort((a, b) => a - b);
  const sortedLongitudes = malls.map((mall) => mall.coordinates[1]).sort((a, b) => a - b);
  const low = Math.floor(malls.length * 0.1);
  const high = Math.ceil(malls.length * 0.9) - 1;
  const focusBounds = L.latLngBounds(
    [sortedLatitudes[low], sortedLongitudes[low]],
    [sortedLatitudes[high], sortedLongitudes[high]],
  ).pad(0.16);

  overviewMap = L.map("overview-map", {
    zoomControl: false,
    attributionControl: false,
    scrollWheelZoom: false,
    dragging: false,
    doubleClickZoom: false,
    boxZoom: false,
    keyboard: false,
    fadeAnimation: false,
  });
  L.tileLayer(basemaps[0].url, { maxZoom: 18 }).addTo(overviewMap);
  for (const mall of malls) {
    L.circleMarker(mall.coordinates, {
      mallName: mall.name,
      radius: 3.5,
      color: "#fff",
      weight: 1,
      fillColor: mallColor(mall),
      fillOpacity: 1,
      bubblingMouseEvents: false,
    }).addTo(overviewMap).on("click", () => showMall(mall));
  }
  overviewViewport = L.rectangle(focusBounds, { color: "#244f49", weight: 1.5, fillColor: "#244f49", fillOpacity: 0.08, interactive: false }).addTo(overviewMap);
  overviewMap.fitBounds(allBounds.pad(0.12), { padding: [8, 8], animate: false });
  overviewMap.on("click", (event) => map.setView(event.latlng, Math.max(map.getZoom(), 12)));
  map.on("moveend", () => {
    overviewViewport.setBounds(map.getBounds());
    if (selected) markers.get(selected.name)?.getElement()?.classList.add("is-selected");
  });

  function fitFocus() {
    map.fitBounds(focusBounds, { padding: [30, 30], maxZoom: 12, animate: false });
  }
  function fitAll() {
    map.fitBounds(allBounds.pad(0.08), { padding: [24, 24], maxZoom: 12, animate: false });
  }
  byId("focus-button").addEventListener("click", fitFocus);
  byId("fit-button").addEventListener("click", () => {
    fitAll();
  });
  fitFocus();
  renderList();
})();
