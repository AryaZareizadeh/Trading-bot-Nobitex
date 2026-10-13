/* One generation owns every request and response body in a dashboard session. */
(function (root) {
  class SessionChanged extends Error {
    constructor() { super('Dashboard session changed'); this.name = 'SessionChanged'; }
  }

  function create({fetch, getToken, isAuthenticated, onUnauthorized}) {
    let generation = 0;
    const controllers = new Set();
    const snapshot = () => ({generation, token:getToken()});
    const isCurrent = owner => owner.generation === generation && owner.token === getToken();
    const changed = () => new SessionChanged();
    const assertCurrent = owner => { if (!isCurrent(owner)) throw changed(); };
    const isStale = error => error?.name === 'SessionChanged';
    async function run(action) {
      const owner = snapshot();
      try { return await action(owner); }
      catch (error) {
        if (!isCurrent(owner) || isStale(error)) return;
        throw error;
      }
    }
    async function wait(owner, promise) {
      try {
        const value = await promise;
        assertCurrent(owner);
        return value;
      } catch (error) {
        assertCurrent(owner);
        throw error;
      }
    }
    function invalidate() {
      generation++;
      for (const controller of controllers) controller.abort();
      controllers.clear();
    }
    async function request(url, options = {}, publicRequest = false) {
      const owner = snapshot();
      if (!publicRequest && !isAuthenticated()) throw changed();
      const controller = typeof AbortController === 'function' ? new AbortController() : null;
      const headers = new Headers(options.headers || {});
      if (!publicRequest && !['GET','HEAD','OPTIONS'].includes((options.method || 'GET').toUpperCase())) {
        headers.set('X-CSRF-Token', owner.token);
      }
      const abort = () => controller?.abort();
      if (options.signal?.aborted) abort();
      options.signal?.addEventListener('abort', abort, {once:true});
      if (controller) controllers.add(controller);
      let response;
      try {
        response = await wait(owner, fetch(url, {...options, headers, credentials:'same-origin',
          ...(controller ? {signal:controller.signal} : {})}));
        if (!publicRequest && response.status === 401) {
          onUnauthorized();
          throw changed();
        }
      } finally {
        if (controller) controllers.delete(controller);
        options.signal?.removeEventListener('abort', abort);
      }
      // Fetch resolves before its body. Keep that second async boundary under
      // the same owner, including delayed parsing and late transport failures.
      return new Proxy(response, {get(target, key) {
        const value = Reflect.get(target, key, target);
        if (['json','text','blob','arrayBuffer','formData'].includes(key) && typeof value === 'function') {
          return async (...args) => {
            assertCurrent(owner);
            if (controller) controllers.add(controller);
            try { return await wait(owner, value.apply(target,args)); }
            finally { if (controller) controllers.delete(controller); }
          };
        }
        return typeof value === 'function' ? value.bind(target) : value;
      }});
    }
    return {snapshot,isCurrent,changed,isStale,run,wait,invalidate,request};
  }
  root.DashboardSession = {create,SessionChanged};
})(typeof window === 'undefined' ? globalThis : window);
