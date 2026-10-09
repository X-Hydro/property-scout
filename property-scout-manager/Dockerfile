FROM eclipse-temurin:17-jre-jammy
WORKDIR /app

# Copy the built JAR
COPY target/property-scout-manager-1.0.0.jar app.jar

# Create required directories
RUN mkdir -p /app/config /data

VOLUME /app/config
VOLUME /data

EXPOSE 7080
ENTRYPOINT ["java", "-jar", "app.jar"]