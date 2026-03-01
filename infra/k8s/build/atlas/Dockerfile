FROM arigaio/atlas:latest AS atlas
FROM alpine:3.20
COPY --from=atlas /atlas /usr/local/bin/atlas
RUN apk add --no-cache postgresql-client
ENTRYPOINT ["atlas"]
