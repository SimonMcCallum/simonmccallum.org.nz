FROM nginx:alpine

# Remove default nginx config
RUN rm /etc/nginx/conf.d/default.conf

# Copy custom nginx config
COPY nginx.conf /etc/nginx/conf.d/simonmccallum.conf

# Copy website files
COPY index.html /usr/share/nginx/html/
COPY academic.html /usr/share/nginx/html/
COPY research.html /usr/share/nginx/html/
COPY ludogogy.html /usr/share/nginx/html/
COPY games.html /usr/share/nginx/html/
COPY trebuchet.html /usr/share/nginx/html/
COPY media.html /usr/share/nginx/html/
COPY style.css /usr/share/nginx/html/
COPY script.js /usr/share/nginx/html/

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
