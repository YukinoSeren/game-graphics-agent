const DB_NAME = 'demo-drafts';
const STORE = 'drafts';

function openDatabase() {
  return new Promise((resolve, reject) => {
    if (!('indexedDB' in globalThis)) {
      reject(new Error('浏览器不支持本地草稿存储'));
      return;
    }
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE, { keyPath: 'requestId' });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('无法打开草稿存储'));
  });
}

async function withStore(mode, action) {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, mode);
    const store = tx.objectStore(STORE);
    let result;
    try {
      result = action(store);
    } catch (error) {
      db.close();
      reject(error);
      return;
    }
    tx.oncomplete = () => {
      db.close();
      resolve(result);
    };
    tx.onerror = () => {
      db.close();
      reject(tx.error);
    };
  });
}

function requestToPromise(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

export function openDraftStore() {
  return {
    async prepare(draft) {
      await withStore('readwrite', (store) => store.put({ ...draft, status: 'pending' }));
      return draft.requestId;
    },
    async get(requestId) {
      const db = await openDatabase();
      try {
        const tx = db.transaction(STORE, 'readonly');
        return await requestToPromise(tx.objectStore(STORE).get(requestId));
      } finally {
        db.close();
      }
    },
    async mark(requestId, status, error) {
      const existing = await this.get(requestId);
      if (!existing) return;
      await withStore('readwrite', (store) =>
        store.put({ ...existing, status, error: error || null }),
      );
    },
    async list() {
      const db = await openDatabase();
      try {
        const tx = db.transaction(STORE, 'readonly');
        return await requestToPromise(tx.objectStore(STORE).getAll());
      } finally {
        db.close();
      }
    },
    async remove(requestId) {
      await withStore('readwrite', (store) => store.delete(requestId));
    },
  };
}
