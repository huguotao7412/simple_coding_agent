import argparse


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="world")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    message = f"Hello, {args.name}!"
    print(message)
    return message


if __name__ == "__main__":
    main()
