import { routes, type VercelConfig } from '@vercel/config/v1';

export const config: VercelConfig = {
  buildCommand: 'python scripts/build_assets.py',
  outputDirectory: 'public',
  rewrites: [
    routes.rewrite('/api/v2/(.*)', '/api/index'),
  ],
  headers: [
    routes.cacheControl('/app/sw.js', { public: true, maxAge: 0 }),
    routes.cacheControl('/app/(.*)\\.(js|css|woff2|png|svg)', {
      public: true,
      maxAge: '1 year',
      immutable: true,
    }),
  ],
};
