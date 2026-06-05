# rink serve Worker

This Worker turns an R2 bucket into a small receive-link service for `rink serve`.

## Setup

1. Edit `wrangler.jsonc` if you want a different Worker name, route, or object prefix.
2. Deploy from the parent directory:
   ```bash
   cd ..
   rink serve --deploy --worker-dir <this-worker-dir>
   ```
   `rink serve --deploy` uploads the local admin token as the Worker secret,
   saves the deployed Worker URL, and saves the matching token locally. If
   Wrangler does not print a URL, rink asks for it.
3. Create receive links with `rink serve`.

The public receive page accepts browser uploads with raw streamed `PUT` requests.
Each uploaded file gets a Worker download URL. By default each download URL is
one-time use and the Durable Object tracks view counts.
