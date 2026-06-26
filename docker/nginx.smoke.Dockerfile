ARG NGINX_IMAGE=nginx:1.30.3-alpine@sha256:1bbb1c7ee25067dafc126f7fa39437876283dfa439bbd12df6a5ecd99dc675e4

FROM ${NGINX_IMAGE}

ENV NGINX_LISTEN_PORT=80
ENV GATEWAY_HOSTNAME=gateway
ENV GATEWAY_INTERNAL_PORT=3000

COPY gateway/nginx.conf /etc/nginx/templates/default.conf.template
COPY dist /usr/share/nginx/html
RUN envsubst '$NGINX_LISTEN_PORT $GATEWAY_HOSTNAME $GATEWAY_INTERNAL_PORT' \
    < /etc/nginx/templates/default.conf.template \
    > /etc/nginx/conf.d/default.conf \
    && nginx -t

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
