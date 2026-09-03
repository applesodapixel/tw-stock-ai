const C='tw-stock-ai-v06';
self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(C).then(c=>c.addAll(['./','./index.html','./manifest.webmanifest'])))});
self.addEventListener('activate',e=>e.waitUntil(Promise.all([self.clients.claim(),caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==C).map(k=>caches.delete(k))))])));
self.addEventListener('fetch',e=>{
 const u=new URL(e.request.url);
 if(u.pathname.includes('/data/')||u.pathname.endsWith('/index.html')||u.pathname.endsWith('/tw-stock-ai/')){
   e.respondWith(fetch(e.request,{cache:'no-store'}).then(r=>{const cp=r.clone();caches.open(C).then(c=>c.put(e.request,cp));return r}).catch(()=>caches.match(e.request)));return;
 }
 e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request)));
});