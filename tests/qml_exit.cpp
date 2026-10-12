#include <QCoreApplication>
#include <QGuiApplication>
#include <QQmlComponent>
#include <QQmlEngine>
#include <QString>
#include <QUrl>

int main(int argc, char **argv) {
    QGuiApplication app(argc, argv);
    if (argc != 2) {
        return 2;
    }
    int code = 0;
    bool exited = false;
    QQmlEngine engine;
    QObject::connect(&engine, &QQmlEngine::exit, &app, [&](int value) {
        exited = true;
        code = value;
        QCoreApplication::exit(value);
    });
    QQmlComponent component(&engine, QUrl::fromLocalFile(QString::fromLocal8Bit(argv[1])));
    if (component.isError()) {
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
