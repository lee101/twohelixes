/* Official-zone ownership and point containment are intentionally distinct. */
(() => {
  const mode=document.getElementById('catchment-mode');
  const year=document.getElementById('catchment-year');
  const category=document.getElementById('catchment-category');
  const status=document.getElementById('catchment-status');
  const future=document.getElementById('catchment-future');
  let data=null, pending=null, generation=0, selected=null, lookupGeneration=0;
  const cache=new Map();
  const palette=['#6589a4','#8c81ae','#799c88','#b39a71','#a78090','#78a0a4'];
  const shade=f=>{let n=0;for(const c of f.properties.school_code)n=(n*31+c.charCodeAt(0))>>>0;return palette[n%palette.length];};
  map.createPane('catchmentAreas');map.getPane('catchmentAreas').style.zIndex=350;
  map.createPane('catchmentFocus');map.getPane('catchmentFocus').style.zIndex=360;
  map.getPane('catchmentFocus').style.pointerEvents='none';
  const overlay=L.geoJSON(null,{pane:'catchmentAreas',style:f=>({color:shade(f),weight:1,opacity:.38,fillColor:shade(f),fillOpacity:.055,dashArray:f.properties.future?'5 4':null}),onEachFeature:(f,l)=>{
    const tip=document.createElement('span');tip.textContent=f.properties.school_name+' · '+f.properties.category+' · '+f.properties.enrolment_year;
    l.bindTooltip(tip);
    l.on('mouseover',()=>highlightFeature(f));
    l.on('mouseout',()=>{if(!selected)clearHighlight();});
  }}).addTo(map);
  const focus=L.geoJSON(null,{pane:'catchmentFocus',interactive:false,style:{color:'#386d89',weight:2,opacity:.85,fillColor:'#7fa8ba',fillOpacity:.13}}).addTo(map);
  function eligible(f){const p=f.properties;return (future.checked||!p.future)&&(!category.value||p.category===category.value)&&(!year.value||p.year_levels.includes(Number(year.value)));}
  function clearHighlight(){generation++;focus.clearLayers();}
  async function fetchJSON(url){const r=await fetch(url);if(!r.ok)throw Error('Boundary data could not be loaded');return r.json();}
  async function load(){if(data)return data;if(!pending)pending=fetchJSON('/v1/schools/nsw/catchments?future=1').then(d=>{data=d;status.textContent=`${d.metadata.feature_count.toLocaleString()} zones · ${d.metadata.matched_school_count.toLocaleString()} linked schools · source ${d.metadata.source_file_date}.`;return d;}).catch(e=>{pending=null;throw e;});return pending;}
  async function highlightSchool(id){
    if(mode.value==='off')return;
    const token=++generation;
    try{
      if(!cache.has(id))cache.set(id,fetchJSON('/v1/schools/nsw/catchments?detail=full&future=1&school_id='+encodeURIComponent(id)).catch(e=>{cache.delete(id);throw e;}));
      const d=await cache.get(id);if(token!==generation)return;
      const features=d.features.filter(eligible);focus.clearLayers();focus.addData(features);
      status.textContent=features.length?`${features[0].properties.school_name}: ${features.length} source boundary record(s). ${d.metadata.enrolment_year} intake zones; verify enrolment with School Finder.`:'No linked boundary for this school and filter. This does not establish enrolment eligibility.';
    }catch(e){if(token===generation)status.textContent=e.message;}
  }
  function highlightFeature(f){if(f.properties.school_ids.length)highlightSchool(f.properties.school_ids[0]);else{clearHighlight();focus.addData(f);status.textContent=f.properties.school_name+': source zone has no verified school-ID link. Display outline is simplified.';}}
  function wireMarker(marker){
    marker.on('click',L.DomEvent.stopPropagation);
    const id=()=>Array.from(markers).find(([,m])=>m===marker)?.[0];
    marker.on('mouseover',()=>{const sid=id();if(sid)highlightSchool(sid);});
    marker.on('mouseout',()=>{if(!selected)clearHighlight();});
    marker.on('popupopen',()=>{selected=id();if(selected)highlightSchool(selected);});
    marker.on('popupclose',()=>{selected=null;clearHighlight();});
  }
  layer.eachLayer(wireMarker);layer.on('layeradd',e=>wireMarker(e.layer));
  const results=document.getElementById('results');
  for(const event of ['mouseover','focusin'])results.addEventListener(event,e=>{const b=e.target.closest('[data-school-id]');if(b)highlightSchool(b.dataset.schoolId);});
  for(const event of ['mouseout','focusout'])results.addEventListener(event,e=>{if(!selected&&!e.target.closest('[data-school-id]')?.contains(e.relatedTarget))clearHighlight();});
  async function redraw(){
    overlay.clearLayers();clearHighlight();
    if(mode.value==='off'){status.textContent='Catchments hidden.';return;}
    if(mode.value==='hover'){status.textContent='Hover or select a school to show its linked source boundary. Click the map to find all intake zones at a point.';if(selected)highlightSchool(selected);return;}
    try{
      const d=await load();if(mode.value!=='overlay')return;
      const b=map.getBounds();
      const visible=d.features.filter(f=>eligible(f)&&f.bbox[0]<=b.getEast()&&f.bbox[2]>=b.getWest()&&f.bbox[1]<=b.getNorth()&&f.bbox[3]>=b.getSouth());
      // At statewide zoom preserve usability; users can zoom to see outlines.
      if(map.getZoom()<8){status.textContent='Zoom in to show subtle catchment outlines. School hover still shows its full boundary.';return;}
      overlay.addData(visible);status.textContent=`${visible.length} zone outlines in view. Colours distinguish areas, not achievement. Click map for all overlaps.`;
      if(selected)highlightSchool(selected);
    }catch(e){status.textContent=e.message;}
  }
  for(const control of [mode,year,category,future])control.addEventListener('change',()=>{lookupGeneration++;redraw();});
  map.on('moveend',()=>{if(mode.value==='overlay')redraw();});
  map.on('click',async e=>{
    if(mode.value==='off')return;
    const token=++lookupGeneration;
    try{
      const params=new URLSearchParams({longitude:e.latlng.lng,latitude:e.latlng.lat});
      if(year.value)params.set('year_level',year.value);
      const d=await fetchJSON('/v1/schools/nsw/catchments/lookup?'+params);
      if(token!==lookupGeneration||mode.value==='off')return;
      const box=document.createElement('div');const heading=document.createElement('strong');heading.textContent='Current intake zones at this point';box.append(heading);
      const matches=d.matches.filter(m=>!category.value||m.geo_category===category.value);
      for(const m of matches){const p=document.createElement('p');p.textContent=`${m.geo_school_name} · ${m.geo_category} · years ${m.geo_year_levels.map(y=>y===0?'K':y).join(', ')}${m.geo_priority?' · priority '+m.geo_priority:''}`;box.append(p);}
      const note=document.createElement('p');note.textContent=matches.length?'All matching records shown; overlapping zones do not imply unrestricted choice. Confirm with School Finder.':'No matching published zone found. This is not a determination of enrolment eligibility.';box.append(note);
      L.popup({maxHeight:260}).setLatLng(e.latlng).setContent(box).openOn(map);
    }catch(e){status.textContent=e.message;}
  });
  redraw();
})();
