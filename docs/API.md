# REST API

Base path `/api/v1`. Interactive docs at `/docs`, OpenAPI at `/openapi.json`.

## Authentication

Every endpoint requires a bearer token — either an **API key** (Settings → API Keys,
`poct_…`) or an **OAuth access token** from the client-credentials flow.

```
Authorization: Bearer poct_xxxxxxxx
```

OAuth token endpoint (RFC 6749 client_credentials):

```bash
curl -X POST http://localhost:8000/oauth/token \
  -d grant_type=client_credentials -d client_id=poct_client_… -d client_secret=…
```

## Endpoints

### Lookups
- `GET|POST /contact-roles/`, `GET|PATCH|DELETE /contact-roles/{id}`
- `GET|POST /project-statuses/`, `…/{id}`
- `GET|POST /feature-types/`, `…/{id}`
- `GET|POST /use-case-statuses/`, `…/{id}`

`is_system` rows and lookups still in use cannot be deleted (409).

### Customers & contacts
- `GET|POST /customers/`, `GET|PATCH|DELETE /customers/{id}` (detail includes contacts)
- `GET|POST /customers/{id}/contacts`, `PATCH|DELETE /customers/contacts/{contact_id}`

### Use-case library
- `GET|POST /use-case-library/`, `GET|PATCH|DELETE /use-case-library/{id}`

### Projects & use cases
- `GET|POST /projects/`, `GET|PATCH|DELETE /projects/{id}` (detail includes use cases)
- `GET /projects/{id}/use-cases`
- `POST /projects/{id}/use-cases` — add an ad-hoc (custom) use case
- `POST /projects/{id}/use-cases/from-library` — `{"library_ids": [...]}`, copies as
  snapshots (de-duplicated)
- `PATCH|DELETE /projects/use-cases/{use_case_id}`

## Notes

- Use cases added from the library are **snapshots**; later edits to the library don't
  change them, and deleting a library entry nulls the `library_id` but keeps the use case.
- List filters: `projects` accepts `status_id`, `customer_id`, `include_archived`;
  lookups and the library accept `is_active`; the library also accepts `category`.
