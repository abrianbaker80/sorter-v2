export type PartCatalogNamespace = 'rebrickable_part_number' | 'bricklink_item_number';
export type PartCatalogStatus = 'resolved' | 'no_image' | 'not_found' | 'temporarily_unavailable';
export type PartCatalogState = 'absent' | 'loading' | PartCatalogStatus;

export type PartCatalogIdentity = {
	part_id: string;
	part_namespace: PartCatalogNamespace;
	color_id?: string | null;
	color_namespace?: string | null;
};
export type PartCatalogResult = {
	requested_part_id: string; requested_part_namespace: PartCatalogNamespace;
	requested_color_id: string | null; requested_color_namespace: string | null;
	canonical_part_id: string | null; canonical_part_namespace: PartCatalogNamespace | null;
	provider_part_id: string | null; name: string | null;
	image_url: string | null; image_source: string | null;
	image_match: 'exact' | 'mapped' | 'none';
	color_specific: boolean; found: boolean;
	status: PartCatalogStatus; stale: boolean;
	retry_after_seconds: number | null;
};
type Request = Required<PartCatalogIdentity> & { baseUrl: string; key: string };
type CachedEntry = { result: PartCatalogResult | null; retryAt: number };
const MAX_BATCH = 250;
const LOCAL_RETRY_SECONDS = 5;
const namespaces = new Set<PartCatalogNamespace>(['rebrickable_part_number', 'bricklink_item_number']);
const statuses = new Set<PartCatalogStatus>(['resolved', 'no_image', 'not_found', 'temporarily_unavailable']);
const imageMatches = new Set(['exact', 'mapped', 'none']);
const publicFields = [
	'requested_part_id', 'requested_part_namespace', 'requested_color_id', 'requested_color_namespace',
	'canonical_part_id', 'canonical_part_namespace', 'provider_part_id', 'name', 'image_url',
	'image_source', 'image_match', 'color_specific', 'found', 'status', 'stale', 'retry_after_seconds'
] as const;
let cache = $state<Map<string, CachedEntry>>(new Map());
const flights = new Map<string, Promise<PartCatalogResult>>();
function normalizeBaseUrl(baseUrl: string): string {
	const parsed = new URL(baseUrl.trim());
	if ((parsed.protocol !== 'http:' && parsed.protocol !== 'https:') || parsed.username || parsed.password)
		throw new TypeError('Catalog base URL must be credential-free HTTP or HTTPS');
	parsed.search = '';
	parsed.hash = '';
	return parsed.toString().replace(/\/+$/, '');
}
function normalize(baseUrl: string, identity: PartCatalogIdentity): Request {
	const partId = identity.part_id;
	if (!partId || partId !== partId.trim() || partId.length > 64 || !namespaces.has(identity.part_namespace))
		throw new TypeError('Invalid part catalog identity');
	if ((identity.color_id == null) !== (identity.color_namespace == null))
		throw new TypeError('color_id and color_namespace must be provided together');
	const colorId = identity.color_id ?? null;
	const colorNamespace = identity.color_namespace ?? null;
	if ((colorId !== null && (!colorId || colorId !== colorId.trim())) ||
		(colorNamespace !== null && (!colorNamespace || colorNamespace !== colorNamespace.trim())))
		throw new TypeError('Color correlation fields must be nonblank');
	return {
		baseUrl,
		part_id: partId,
		part_namespace: identity.part_namespace,
		color_id: colorId,
		color_namespace: colorNamespace,
		key: JSON.stringify([baseUrl, identity.part_namespace, partId, 'generic'])
	};
}
function emptyResult(request: Request, status: 'not_found' | 'temporarily_unavailable', retryAfter: number | null = null): PartCatalogResult {
	return {
		requested_part_id: request.part_id, requested_part_namespace: request.part_namespace,
		requested_color_id: null, requested_color_namespace: null,
		canonical_part_id: null, canonical_part_namespace: null,
		provider_part_id: null, name: null,
		image_url: null, image_source: null, image_match: 'none',
		color_specific: false, found: false, status, stale: false,
		retry_after_seconds: retryAfter
	};
}
function correlate(result: PartCatalogResult, request: Request): PartCatalogResult {
	return {
		...result,
		requested_part_id: request.part_id, requested_part_namespace: request.part_namespace,
		requested_color_id: request.color_id, requested_color_namespace: request.color_namespace
	};
}
function isObject(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null && !Array.isArray(value); }
function nullableString(value: unknown): value is string | null { return value === null || typeof value === 'string'; }
function parseResult(value: unknown, request: Request, colorId: string | null = null, colorNamespace: string | null = null): PartCatalogResult | null {
	if (!isObject(value)) return null;
	const strings = [
		value.canonical_part_id, value.provider_part_id, value.name, value.image_url, value.image_source
	];
	const retry = value.retry_after_seconds;
	if (
		value.requested_part_id !== request.part_id ||
		value.requested_part_namespace !== request.part_namespace ||
		value.requested_color_id !== colorId || value.requested_color_namespace !== colorNamespace ||
		!strings.every(nullableString) ||
		!(value.canonical_part_namespace === null || namespaces.has(value.canonical_part_namespace as PartCatalogNamespace)) ||
		!imageMatches.has(value.image_match as string) ||
		value.color_specific !== false ||
		typeof value.found !== 'boolean' ||
		!statuses.has(value.status as PartCatalogStatus) ||
		typeof value.stale !== 'boolean' ||
		!(retry === null || (Number.isInteger(retry) && (retry as number) >= 0))
	) return null;
	return Object.fromEntries(publicFields.map((field) => [field, value[field]])) as PartCatalogResult;
}
function genericResult(result: PartCatalogResult): PartCatalogResult {
	return { ...result, requested_color_id: null, requested_color_namespace: null };
}
function retryAfter(response: Response): number | null {
	const raw = response.headers.get('Retry-After');
	if (raw === null || !/^\d+$/.test(raw.trim())) return null;
	const seconds = Number(raw);
	return Number.isSafeInteger(seconds) ? seconds : null;
}
function write(requests: Request[], results: PartCatalogResult[] | null): void {
	const next = new Map(cache);
	requests.forEach((request, index) => {
		const result = results?.[index] ?? null;
		const retry = result?.status === 'temporarily_unavailable'
			? (result.retry_after_seconds ?? LOCAL_RETRY_SECONDS) : 0;
		next.set(request.key, { result, retryAt: Date.now() + retry * 1000 });
	});
	cache = next;
}
function save(requests: Request[], results: PartCatalogResult[]): PartCatalogResult[] { write(requests, results); return results; }
function usable(request: Request): PartCatalogResult | null {
	const entry = cache.get(request.key);
	if (!entry?.result) return null;
	return entry.result.status !== 'temporarily_unavailable' || Date.now() < entry.retryAt
		? entry.result : null;
}
function track(request: Request, promise: Promise<PartCatalogResult>): Promise<PartCatalogResult> {
	flights.set(request.key, promise);
	const clear = () => { if (flights.get(request.key) === promise) flights.delete(request.key); };
	void promise.then(clear, clear);
	return promise;
}
async function requestSingle(request: Request): Promise<PartCatalogResult> {
	write([request], null);
	try {
		const query = new URLSearchParams({ part_namespace: request.part_namespace });
		if (request.color_id !== null && request.color_namespace !== null) {
			query.set('color_id', request.color_id);
			query.set('color_namespace', request.color_namespace);
		}
		const response = await fetch(`${request.baseUrl}/api/pieces/catalog/${encodeURIComponent(request.part_id)}?${query}`);
		if (response.status === 404) {
			const body: unknown = await response.json().catch(() => null);
			if (isObject(body) && isObject(body.detail) && body.detail.code === 'PART_CATALOG_NOT_FOUND') {
				return save([request], [emptyResult(request, 'not_found')])[0];
			}
		}
		if (!response.ok) return save([request], [
			emptyResult(request, 'temporarily_unavailable', retryAfter(response))
		])[0];
		const parsed = parseResult(await response.json(), request, request.color_id, request.color_namespace);
		const result = parsed ? genericResult(parsed) : emptyResult(request, 'temporarily_unavailable');
		return save([request], [result])[0];
	} catch {
		return save([request], [emptyResult(request, 'temporarily_unavailable')])[0];
	}
}
function temporaryResults(requests: Request[], retry: number | null = null): PartCatalogResult[] { return requests.map((request) => emptyResult(request, 'temporarily_unavailable', retry)); }
async function requestBatch(requests: Request[]): Promise<PartCatalogResult[]> {
	write(requests, null);
	try {
		const response = await fetch(`${requests[0].baseUrl}/api/pieces/catalog/resolve`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ parts: requests.map(({ part_id, part_namespace }) => ({ part_id, part_namespace })) })
		});
		if (!response.ok) return save(requests, temporaryResults(requests, retryAfter(response)));
		const body: unknown = await response.json();
		const rows = isObject(body) && Array.isArray(body.results) ? body.results : null;
		if (rows?.length !== requests.length) return save(requests, temporaryResults(requests));
		return save(requests, requests.map((request, index) =>
			parseResult(rows[index], request) ?? emptyResult(request, 'temporarily_unavailable')
		));
	} catch {
		return save(requests, temporaryResults(requests));
	}
}
function requestFor(baseUrl: string, identity: PartCatalogIdentity): Request { return normalize(normalizeBaseUrl(baseUrl), identity); }
async function resolve(baseUrl: string, identity: PartCatalogIdentity): Promise<PartCatalogResult> {
	const request = requestFor(baseUrl, identity);
	const hit = usable(request);
	if (hit) return correlate(hit, request);
	let flight = flights.get(request.key);
	if (!flight) flight = track(request, Promise.resolve().then(() => requestSingle(request)));
	return correlate(await flight, request);
}
async function resolveMany(baseUrl: string, identities: PartCatalogIdentity[]): Promise<PartCatalogResult[]> {
	const base = normalizeBaseUrl(baseUrl);
	const requests = identities.map((identity) => normalize(base, identity));
	const unique = new Map(requests.map((request) => [request.key, request]));
	const pending = new Map<string, Promise<PartCatalogResult>>();
	const misses: Request[] = [];
	for (const request of unique.values()) {
		const hit = usable(request);
		const flight = flights.get(request.key);
		if (hit) pending.set(request.key, Promise.resolve(hit));
		else if (flight) pending.set(request.key, flight);
		else misses.push(request);
	}
	for (let offset = 0; offset < misses.length; offset += MAX_BATCH) {
		const chunk = misses.slice(offset, offset + MAX_BATCH);
		const batch = Promise.resolve().then(() => requestBatch(chunk));
		chunk.forEach((request, index) => {
			pending.set(request.key, track(request, batch.then((results) => results[index])));
		});
	}
	return Promise.all(requests.map(async (request) =>
		correlate(await pending.get(request.key)!, request)
	));
}
export const partCatalog = {
	get(baseUrl: string, identity: PartCatalogIdentity): PartCatalogResult | null {
		const request = requestFor(baseUrl, identity);
		const result = cache.get(request.key)?.result;
		return result ? correlate(result, request) : null;
	},
	state(baseUrl: string, identity: PartCatalogIdentity): PartCatalogState {
		const entry = cache.get(requestFor(baseUrl, identity).key);
		return entry ? (entry.result?.status ?? 'loading') : 'absent';
	},
	resolve,
	resolveMany
};
