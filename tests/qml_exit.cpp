#include <cstdio>
#include <QCoreApplication>
#include <QGuiApplication>
#include <QQmlComponent>
#include <QQmlEngine>
#include <QString>
#include <QUrl>

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "qml-runner: expected the QML file path\n");
        return 2;
    }
    const QString file = QString::fromLocal8Bit(argv[1]);
    QGuiApplication app(argc, argv);
    int code = 0;
    bool exited = false;
    QQmlEngine engine;
    QObject::connect(&engine, &QQmlEngine::exit, &app, [&](int value) {
        exited = true;
        code = value;
        QCoreApplication::exit(value);
    });
    QQmlComponent component(&engine, QUrl::fromLocalFile(file));
    if (component.isError()) {
        fprintf(stderr, "%s\n", qPrintable(component.errorString()));
        return 2;
    }
    QObject *object = component.create();
    if (exited) {
        return code;
    }
    if (object == nullptr) {
        return 3;
    }
    return app.exec();
}
